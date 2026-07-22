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
        """根据代码类型返回有效的参数集（ETF使用ETF_T0_PARAMS覆盖，个股使用STOCK_PARAMS覆盖）"""
        if self._is_etf(code):
            # V1.12: ETF参数以PARAMS为基，ETF_T0_PARAMS覆盖，避免KeyError
            return {**PARAMS, **ETF_T0_PARAMS}
        # V1.24: 个股专属参数覆盖（回测驱动优化）
        # 先取全局PARAMS，再用STOCK_PARAMS中的个股配置覆盖
        stock_params = STOCK_PARAMS.get(code, {})
        if stock_params:
            return {**PARAMS, **stock_params}
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

    def _dynamic_threshold(self, side: str, price: float, vwap: float, rsi: float, vol_ratio: float, holding: dict, market_state: str, is_strong_pullback: bool = False, code: str = "") -> int:
        memory = _strategy_memory_for_code(code)
        p = self._get_params(code)
        is_etf = holding.get("type") == "etf"
        base = 40 if is_etf else 45
        if side == "buy":
            base += int(memory.get("buy_threshold_adj", 0))
        else:
            base += int(memory.get("sell_threshold_adj", 0))
        if side == "buy":
            if price < vwap:
                base -= 2
            if rsi <= p["rsi_oversold"]:
                base -= 2
            if vol_ratio >= p["vol_ratio_confirm"]:
                base -= 1
            if market_state == "trend_down":
                # V1.8fix: 趋势下跌时不再固定惩罚，而是基于 VWAP 偏离度浮动
                # 当价格已充分低于 VWAP 时，降低门槛鼓励低吸；反之才提高门槛
                vwap_deviation = (vwap - price) / vwap if vwap else 0.0
                if vwap_deviation > 0.01:
                    base -= 2  # 已充分下跌，降低门槛奖励低吸
                elif vwap_deviation > 0.005:
                    base += 1  # 小幅下跌，轻微提高门槛
                else:
                    base += p["market_state_threshold_bias"] + 1  # 价格仍在高位，正常惩罚
            elif market_state == "trend_up" and not is_strong_pullback:
                base += 3
            elif market_state == "range_bound":
                base += 1
        else:
            if price > vwap:
                base -= 3
            if rsi >= p["rsi_overbought"]:
                base -= 3
            if vol_ratio >= p["vol_ratio_confirm"]:
                base -= 2
            if market_state == "trend_down":
                base -= 2
            elif market_state == "trend_up":
                base += 2
        if vwap and abs(price - vwap) / vwap < 0.002:
            base += 2
        if side == "buy" and not is_strong_pullback:
            base += 1
        # V1.12: ETF动态阈值上限，防止惩罚叠加导致无法触发
        if is_etf:
            base = min(base, p.get("etf_threshold_cap", 38))
        return max(35, min(60, base))

    def evaluate(self, code, name, df, holding, daily_ctx=None):
        # V1.19: 早盘10分钟内允许5根数据即可评估，捕捉开盘弱势反弹
        min_bars = 5 if len(df) >= 5 and len(df) < 15 and pd.to_datetime(df.iloc[-1]["date"]).time() <= pd.to_datetime("09:40:00").time() else 15
        if df.empty or len(df) < min_bars:
            return 0, 0, None

        # V1.12: 在 evaluate 顶部定义 is_etf，避免后续引用时 NameError
        is_etf = holding.get("type") == "etf"

        daily_ctx = daily_ctx if isinstance(daily_ctx, dict) else _default_daily_context(code)

        # 【V1.16】多周期上下文加载：基于腾讯快照QT（日线/周线/月线）
        # 当 daily_context 不可用时，通过多周期数据推断趋势方向
        multi_tf = None
        multi_tf_dict = {}
        try:
            if 'MultiTimeframeFetcher' in globals():
                multi_tf = MultiTimeframeFetcher().build_context(code)
                multi_tf_dict = multi_tf.to_dict()
        except Exception:
            multi_tf_dict = {}

        minute_status = MINUTE_FETCH_STATUS.get(code, "unknown")
        if minute_status not in {"ok", "cache_hit"}:
            return 0, 0, None

        self._reset_daily_state_if_needed()
        p = self._get_params(code)

        diag = self.diagnostics.setdefault(code, {})  # V1.14fix: diag 提前定义，避免第352行引用时未绑定
        indicators = {}  # V1.23fix-R2: 提前初始化，避免5分钟 bullish_reversal 块引用时未绑定
        last = df.iloc[-1]
        prev = df.iloc[-2]
        _dt = pd.to_datetime(last["time"])
        t_val = _dt.hour * 100 + _dt.minute
        cached_minute_df = None
        cached_15m_df = None
        cached_5m_df = None
        try:
            backtest_cache = globals().get("BACKTEST_DAY_CACHE", {})
            if isinstance(backtest_cache, dict):
                cache_key = str(pd.to_datetime(last["time"]).strftime("%Y-%m-%d"))
                cache = backtest_cache.get(cache_key)
                if isinstance(cache, dict):
                    cached_minute_df = cache.get("minute_indicators")
                    cached_15m_df = cache.get("resample_15m")
                    cached_5m_df = cache.get("resample_5m")
        except Exception:
            cached_minute_df = None
            cached_15m_df = None
            cached_5m_df = None

        # V1.26: T模式切换（优先使用动态 daily_ctx 注入）
        t_mode = str((daily_ctx or {}).get("t_mode") or (daily_ctx or {}).get("effective_t_mode") or "")
        t_mode_source = "daily_ctx" if t_mode else ""
        if t_mode not in {"long", "short"}:
            t_mode = ""
        if not t_mode:
            if 'T_MODE' in globals() and isinstance(T_MODE, dict):
                t_mode = T_MODE.get(code, "long")
                t_mode_source = "global_T_MODE"
            elif 'load_t_mode' in globals():
                try:
                    t_mode = load_t_mode().get(code, "long")
                    t_mode_source = "t_mode_file"
                except Exception:
                    t_mode = "long"
                    t_mode_source = "fallback"
        if t_mode not in {"long", "short"}:
            t_mode = "long"
        diag["t_mode"] = t_mode
        diag["t_mode_source"] = t_mode_source or "fallback"
        diag["t_trade_gate"] = (daily_ctx or {}).get("t_trade_gate", "normal")
        diag["t_pos_factor"] = float((daily_ctx or {}).get("t_pos_factor", 1.0) or 0.0)
        diag["t_reason"] = (daily_ctx or {}).get("t_reason", "")
        is_short_mode = t_mode == "short"
        if is_short_mode:
            # 反T模式：应用short专属参数覆盖
            p = {**p, **SHORT_MODE_PARAMS}
            # 反T必须有底仓才能做（先卖后买）
            hold_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
            if hold_qty <= 0:
                # 无持仓不做反T
                return 0, 0, None

        # V1.24: 计算压力位/支撑位（diag/last/indicators已初始化）
        price = float(last["close"]) if "close" in last else 0.0
        ps = _calc_ps_levels(price, daily_ctx) if price > 0 else {}
        diag["pressure_support"] = ps
        indicators["pressure_support"] = ps
        # 将压力/支撑位信息写入决策trace，供回测和飞书打印
        _append_jsonl(_trace_path("decision_trace"), {
            "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code,
            "name": name,
            "event": "pressure_support_calc",
            "pressure_name": ps.get("pressure_name", ""),
            "pressure_level": ps.get("pressure_level", 0.0),
            "pressure_gap": ps.get("pressure_gap", 0.0),
            "support_name": ps.get("support_name", ""),
            "support_level": ps.get("support_level", 0.0),
            "support_gap": ps.get("support_gap", 0.0),
            "is_major_pressure": ps.get("is_major_pressure", False),
            "sell_qty_pct": ps.get("sell_qty_pct", 100),
        })

        # V1.25: 早盘特征计算与预警检查
        alert_level = 0
        triggered_rules = []
        morning_stats = {}
        if t_val <= 1000:  # 仅在10:00前计算早盘特征
            alert_level, triggered_rules, morning_stats = self._calc_morning_features_and_alert(code, df, t_val)
        # V1.25: 早盘预警状态持久化（10:00后若触发过L2，持续检查是否可纠正）
        mas = self.morning_alert_state.get(code, {})
        if alert_level > 0:
            # 首次触发或级别升级时发送飞书预警
            prev_level = mas.get("level", 0)
            if prev_level < alert_level:
                try:
                    send_morning_alert(code, name, alert_level, triggered_rules, morning_stats)
                except Exception:
                    pass
            self.morning_alert_state[code] = {
                "level": alert_level,
                "rules": triggered_rules,
                "stats": morning_stats,
                "triggered_at": t_val,
                "corrected": False,
            }
        elif mas.get("level", 0) >= 2 and t_val >= 1130 and not mas.get("corrected", False):
            # 检查是否满足纠正条件
            corrected, reason = self._check_morning_correction(code, df, t_val)
            if corrected:
                mas["corrected"] = True
                mas["correction_reason"] = reason
                mas["corrected_at"] = t_val
                self.morning_alert_state[code] = mas
                # 飞书通知：纠正解除
                try:
                    notify_alert_cleared(code, name, reason, morning_stats)
                except Exception:
                    pass
        # 应用当前有效的alert级别
        effective_alert = self.morning_alert_state.get(code, {}).get("level", 0)
        if self.morning_alert_state.get(code, {}).get("corrected", False):
            effective_alert = 0  # 已纠正，恢复正常
        indicators["morning_alert_level"] = effective_alert
        indicators["morning_alert_rules"] = [r.get("name", "") for r in self.morning_alert_state.get(code, {}).get("rules", [])]
        indicators["morning_stats"] = morning_stats

        memory = _strategy_memory_for_code(code)
        starvation_state = load_starvation_state().get(code, {})
        starvation_days = int(starvation_state.get("days", 0) or 0)
        starvation_relax_until = str(starvation_state.get("relax_until", "") or "")
        starvation_relax_active = bool(starvation_relax_until and starvation_relax_until >= get_today_str())
        buy_confirm_min_score = int(memory.get("buy_confirm_min_score", p["buy_confirm_min_score"]))
        buy_confirm_min_factors = int(memory.get("buy_confirm_min_factors", p["buy_confirm_min_factors"]))
        buy_confirm_min_seconds = int(memory.get("buy_confirm_min_seconds", p["buy_confirm_min_seconds"]))
        buy_rebound_min_score_gap = int(memory.get("buy_rebound_min_score_gap", p["buy_rebound_min_score_gap"]))
        if starvation_relax_active and starvation_days >= p["buy_starvation_days"]:
            buy_confirm_min_seconds = max(20, buy_confirm_min_seconds - p["buy_starvation_relax_seconds"])
            buy_confirm_min_factors = max(2, buy_confirm_min_factors - p["buy_starvation_relax_factors"])
            buy_rebound_min_score_gap = max(2, buy_rebound_min_score_gap - p["buy_starvation_relax_gap"])
        sell_confirm_min_factors = int(memory.get("sell_confirm_min_factors", p["sell_confirm_min_factors"]))
        sell_confirm_min_seconds = int(memory.get("sell_confirm_min_seconds", p["sell_confirm_min_seconds"]))
        buy_needs_momentum = bool(memory.get("buy_needs_momentum", True))
        buy_needs_ema = bool(memory.get("buy_needs_ema", True))
        buy_needs_volume = bool(memory.get("buy_needs_volume", True))
        sell_needs_momentum = bool(memory.get("sell_needs_momentum", True))
        sell_needs_ema = bool(memory.get("sell_needs_ema", True))
        sell_needs_volume = bool(memory.get("sell_needs_volume", True))
        buy_min_time = str(memory.get("buy_min_time", "09:40"))

        price = float(last["close"])
        vwap = float(last["vwap"])
        day_amplitude = float(last["day_amplitude"]) if pd.notna(last["day_amplitude"]) else 0.0
        current_minute = _dt.hour * 60 + _dt.minute
        try:
            min_hour, min_minute = [int(x) for x in buy_min_time.split(":", 1)]
            min_trade_minute = min_hour * 60 + min_minute
        except Exception:
            min_trade_minute = 9 * 60 + 40

        # V1.26: 低点抬高支撑确认 — 当早盘预警触发但出现低点抬高信号时，降级alert级别
        # 因为低点抬高说明承接有力，并非单边下跌
        higher_low_detected, higher_low_detail = self._check_higher_low_support(code, df, price, vwap)
        indicators["higher_low_support_detected"] = higher_low_detected
        indicators["higher_low_support_detail"] = higher_low_detail
        if higher_low_detected and effective_alert >= 1:
            # 承接确认：直接清除预警（因为低点抬高证明早盘并非单边下跌）
            effective_alert = 0
            # 同时标记为已纠正，防止后续再次触发
            mas = self.morning_alert_state.get(code, {})
            if mas.get("level", 0) >= 1:
                mas["corrected"] = True
                mas["correction_reason"] = "低点抬高支撑确认"
                mas["corrected_at"] = t_val
                self.morning_alert_state[code] = mas
            indicators["morning_alert_level"] = 0
            indicators["morning_alert_downgrade_reason"] = "低点抬高支撑确认，承接有力，清除预警"

        # V1.20: 早盘卖出确认门槛调整
        # 个股9:30-9:40适当降低（给冲高机会发育时间），但ETF不降低甚至提高
        if 930 <= t_val <= 940:
            if is_etf:
                # ETF早盘卖出需更严格确认（5分钟滞涨+5个因子）
                sell_confirm_min_seconds = max(60, sell_confirm_min_seconds + 20)
                sell_confirm_min_factors = max(5, sell_confirm_min_factors + 1)
            else:
                sell_confirm_min_seconds = max(20, sell_confirm_min_seconds - 40)
                sell_confirm_min_factors = max(4, sell_confirm_min_factors - 3)

        rsi = float(last["rsi"]) if pd.notna(last["rsi"]) else 50
        bb_pct = float(last["bb_pct"]) if pd.notna(last["bb_pct"]) else 0.5
        macd_hist = float(last["macd_hist"]) if pd.notna(last["macd_hist"]) else 0.0
        prev_macd_hist = float(prev["macd_hist"]) if pd.notna(prev["macd_hist"]) else 0.0
        ema_spread = float(last["ema_spread"]) if pd.notna(last["ema_spread"]) else 0.0
        prev_ema_spread = float(prev["ema_spread"]) if pd.notna(prev["ema_spread"]) else 0.0
        range_pos = float(last["range_pos"]) if pd.notna(last["range_pos"]) else 0.5

        vol_ratio = float(last["vol_ratio"]) if pd.notna(last["vol_ratio"]) else 1.0
        mom5 = float(last["mom5"]) if pd.notna(last["mom5"]) else 0.0
        lower_shadow = float(last["lower_shadow"]) if pd.notna(last["lower_shadow"]) else 0.0
        upper_shadow = float(last["upper_shadow"]) if pd.notna(last["upper_shadow"]) else 0.0

        # V1.15: 修复数据源 upper_shadow 缺失（akshare 分钟线 high=close 时近似）
        if upper_shadow <= 0.01 and len(df) >= 5:
            recent_5 = df.iloc[-5:]
            recent_high = float(recent_5["high"].max())
            recent_low = float(recent_5["low"].min())
            if recent_high > price * 1.001 and recent_high > recent_low:
                upper_shadow_approx = (recent_high - price) / (recent_high - recent_low)
                if upper_shadow_approx > upper_shadow:
                    upper_shadow = upper_shadow_approx
                    diag["upper_shadow_approx"] = True

        # ==================== 15分钟线分析（低吸优化 V1.13） ====================
        if isinstance(cached_15m_df, pd.DataFrame) and not cached_15m_df.empty:
            cutoff_15m = pd.to_datetime(last["time"]).floor("15min")
            df_15min = cached_15m_df[cached_15m_df["time"] <= cutoff_15m].copy()
        else:
            df_15min = resample_to_15min(df)
            df_15min = add_15min_indicators(df_15min)

        # 15分钟指标默认值
        rsi_15m = 50.0
        macd_hist_15m = 0.0
        prev_macd_hist_15m = 0.0
        ema_spread_15m = 0.0
        prev_ema_spread_15m = 0.0
        vol_ratio_15m = 1.0
        mom2_15m = 0.0
        is_kinetic_exhaustion = False
        is_near_15m_support = False
        is_multi_bottom_15m = False
        support_level_15m = 0.0

        if not df_15min.empty and len(df_15min) >= PARAMS.get("min_15min_bars", 3):
            last_15m = df_15min.iloc[-1]
            prev_15m = df_15min.iloc[-2] if len(df_15min) >= 2 else last_15m

            rsi_15m = float(last_15m["rsi_15m"]) if pd.notna(last_15m.get("rsi_15m")) else 50.0
            macd_hist_15m = float(last_15m["macd_hist_15m"]) if pd.notna(last_15m.get("macd_hist_15m")) else 0.0
            prev_macd_hist_15m = float(prev_15m["macd_hist_15m"]) if pd.notna(prev_15m.get("macd_hist_15m")) else 0.0
            ema_spread_15m = float(last_15m["ema_spread_15m"]) if pd.notna(last_15m.get("ema_spread_15m")) else 0.0
            prev_ema_spread_15m = float(prev_15m["ema_spread_15m"]) if pd.notna(prev_15m.get("ema_spread_15m")) else 0.0
            vol_ratio_15m = float(last_15m["vol_ratio_15m"]) if pd.notna(last_15m.get("vol_ratio_15m")) else 1.0
            mom2_15m = float(last_15m["mom2_15m"]) if pd.notna(last_15m.get("mom2_15m")) else 0.0

            # 下跌动能衰竭：MACD负区拐头 + 跌幅收敛 + 缩量
            is_kinetic_exhaustion = (
                macd_hist_15m > prev_macd_hist_15m and
                macd_hist_15m < 0 and
                mom2_15m > -0.015 and
                vol_ratio_15m < 1.3
            )

            # 15分钟强支撑：最近4根15分钟线低点（约1小时）
            if len(df_15min) >= 4:
                recent_lows = df_15min["low"].tail(4).values
                support_level_15m = float(np.min(recent_lows)) if len(recent_lows) > 0 else 0.0
                if support_level_15m > 0:
                    # 当前价格接近支撑（±0.3%）
                    is_near_15m_support = (
                        price <= support_level_15m * 1.003 and
                        price >= support_level_15m * 0.995
                    )
                    # 多重底：最近4根中多次测试同一支撑
                    low_count = sum(
                        1 for low_val in recent_lows
                        if abs(float(low_val) - support_level_15m) / support_level_15m < 0.003
                    )
                    is_multi_bottom_15m = low_count >= 2

        # V1.14: 5分钟线分析 — 低吸时确认量能缩量 + 企稳反转
        if isinstance(cached_5m_df, pd.DataFrame) and not cached_5m_df.empty:
            cutoff_5m = pd.to_datetime(last["time"]).floor("5min")
            df_5min = cached_5m_df[cached_5m_df["time"] <= cutoff_5m].copy()
        else:
            df_5min = resample_to_5min(df)
            df_5min = add_5min_indicators(df_5min)
        
        # 5分钟指标默认值
        vol_ratio_5m = 1.0
        mom2_5m = 0.0
        macd_hist_5m = 0.0
        prev_macd_hist_5m = 0.0
        is_low_rising_5m = False
        is_stop_falling_5m = False
        is_volume_reversal = False  # V1.19fix: 确保始终有定义
        # V1.22fix: 在5分钟K线检查前初始化，防止df_5min不足5根时未定义
        is_strong_bullish_reversal = False
        
        if not df_5min.empty and len(df_5min) >= 3:
            last_5m = df_5min.iloc[-1]
            prev_5m = df_5min.iloc[-2] if len(df_5min) >= 2 else last_5m
            
            vol_ratio_5m = float(last_5m["vol_ratio_5m"]) if pd.notna(last_5m.get("vol_ratio_5m")) else 1.0
            mom2_5m = float(last_5m["mom2_5m"]) if pd.notna(last_5m.get("mom2_5m")) else 0.0
            macd_hist_5m = float(last_5m["macd_hist_5m"]) if pd.notna(last_5m.get("macd_hist_5m")) else 0.0
            prev_macd_hist_5m = float(prev_5m["macd_hist_5m"]) if pd.notna(prev_5m.get("macd_hist_5m")) else 0.0
            is_low_rising_5m = bool(last_5m.get("low_rising_5m", False))
            is_stop_falling_5m = bool(last_5m.get("stop_falling_5m", False))
            
            # V1.17: 缩量止跌+放量反攻检测 — 基于用户7月6日反馈
            # 放宽版：前4根中至少2根阴线，整体高点呈下降或震荡，当前收阳/十字星，价<VWAP，量不极端萎缩
            is_volume_reversal = False
            vr_bearish_count = 0
            vr_high_declining = False
            # V1.22: 大阳线反包确认（7月8日反馈）— 严格版：放量下杀后必须出现大阳线才能确认抄底
            is_strong_bullish_reversal = False
            if len(df_5min) >= 5:
                prev4 = df_5min.iloc[-5:-1]  # 前4根5分钟K线
                # 前4根中阴线数量（close < open）
                vr_bearish_count = sum(1 for _, r in prev4.iterrows() if r["close"] < r["open"])
                # 高点是否整体下降（前4根最高点 > 当前K线高点，或逐步降低）
                highs = [r["high"] for _, r in prev4.iterrows()]
                prev4_high = max(highs) if highs else 0
                current_high = last_5m["high"]
                # 条件1：逐步降低（允许0.3%的波动）
                vr_high_declining = all(highs[i] <= highs[i-1] * 1.003 for i in range(1, len(highs)))
                # 条件2：前4根整体高点高于当前K线高点（确认下降趋势）
                # 修复：只要前4根的最高点 > 当前K线的高点（允许0.1%误差）即可
                high_declining_loose = prev4_high > current_high * 0.999 if current_high > 0 else False
                # 当前K线是否收阳线/十字星（close >= open，或十字星允许微小偏差）
                current_bullish = last_5m["close"] >= last_5m["open"] * 0.9995
                # 价格是否低于VWAP（低吸确认）
                price_below_vwap = price < vwap * 0.995 if vwap else False
                # 成交量条件：不极端萎缩
                # 十字星/阳线区分：十字星（close≈open）时量条件更宽松
                prev4_volumes = [r["volume"] for _, r in prev4.iterrows()]
                prev4_vol_mean = sum(prev4_volumes) / len(prev4_volumes) if prev4_volumes else 0
                is_doji = abs(last_5m["close"] - last_5m["open"]) / last_5m["open"] < 0.001 if last_5m["open"] > 0 else False
                # 十字星：当前量 >= 前4根均量的15%（放量下跌后十字星，量自然萎缩但不到极端）
                # 阳线：当前量 >= 前4根均量的50%（缩量后放量反攻）
                vol_threshold = 0.15 if is_doji else 0.50
                vol_ok = last_5m["volume"] >= prev4_vol_mean * vol_threshold if prev4_vol_mean > 0 else True
                
                # 放宽触发：前4根>=2根阴线 + (高点严格下降 或 高点整体高于当前) + 当前收阳/十字星 + 价<VWAP + 量不极端萎缩
                if (current_bullish and vr_bearish_count >= 2 and 
                    (vr_high_declining or high_declining_loose) and 
                    price_below_vwap and vol_ok):
                    is_volume_reversal = True
                
                # V1.22: 大阳线反包确认 — 更严格的抄底信号
                # 要求：前4根>=2根阴线 + 高点下降 + 当前大阳线 + 放量 + 反包前高
                if (vr_bearish_count >= 2 and (vr_high_declining or high_declining_loose) and
                    price_below_vwap and prev4_vol_mean > 0):
                    # 大阳线判定
                    _5m_pct = (last_5m["close"] - last_5m["open"]) / last_5m["open"] if last_5m["open"] > 0 else 0
                    _5m_amplitude = (last_5m["high"] - last_5m["low"]) / last_5m["low"] if last_5m["low"] > 0 else 0
                    _5m_body = abs(last_5m["close"] - last_5m["open"]) / last_5m["low"] if last_5m["low"] > 0 else 0
                    _is_big_bullish = (
                        last_5m["close"] > last_5m["open"]  # 真阳线，非十字星
                        and _5m_pct >= p.get("bullish_reversal_min_pct", 0.01)  # 涨幅>=1%
                        and (_5m_body / (_5m_amplitude + 1e-9)) >= p.get("bullish_reversal_body_ratio", 0.60)  # 实体饱满
                        and last_5m["volume"] >= prev4_vol_mean * p.get("bullish_reversal_vol_multiplier", 1.0)  # 放量
                        and last_5m["close"] >= prev4_high * p.get("bullish_reversal_engulf", 0.995)  # 反包前高
                    )
                    if _is_big_bullish:
                        is_strong_bullish_reversal = True
                        indicators["is_strong_bullish_reversal"] = True
                        indicators["bullish_reversal_pct"] = _5m_pct
                        indicators["bullish_reversal_body"] = _5m_body / (_5m_amplitude + 1e-9)
        
        if isinstance(cached_minute_df, pd.DataFrame) and not cached_minute_df.empty:
            day_rows = cached_minute_df[cached_minute_df["date"] == last["date"]]
            today_open = float(day_rows.iloc[0]["open"])
        else:
            today_open = float(df[df["date"] == last["date"]].iloc[0]["open"])
        h = HOLDINGS.get(code, {})
        pre_close = h.get("pre_close", today_open)
        today_ret = (price - pre_close) / pre_close if pre_close > 0 else 0.0
        open_gap = (today_open - pre_close) / pre_close if pre_close > 0 else 0.0
        prev_high = float(last["prev_high"]) if pd.notna(last["prev_high"]) else price
        is_strong_trend = (today_ret > 0.035) and (price >= prev_high * 0.99) and (vol_ratio > 1.2)
        is_strong_pullback = is_strong_trend and abs((price - vwap) / vwap) < 0.005 if vwap else False

        benchmark = _resolve_benchmark_snapshot(code, holding)
        market_state = self._classify_market_state(today_ret, price, vwap, vol_ratio, day_amplitude, ema_spread, code)
        benchmark = benchmark if isinstance(benchmark, dict) else {}
        benchmark_state = benchmark.get("benchmark_state", "unknown")
        benchmark_gate = benchmark.get("benchmark_gate", "neutral")
        benchmark_reason = benchmark.get("benchmark_gate_reason", "")
        daily_ctx = daily_ctx if isinstance(daily_ctx, dict) else {}
        daily_status = daily_ctx.get("daily_status", "unknown")
        daily_gate = daily_ctx.get("daily_gate", "neutral")
        daily_trend_bg = daily_ctx.get("daily_trend_bg", "unknown")
        daily_ma5 = float(daily_ctx.get("daily_ma5", 0.0) or 0.0)
        daily_ma5_slope = float(daily_ctx.get("daily_ma5_slope", 0.0) or 0.0)
        daily_above_ma5 = bool(daily_ctx.get("daily_above_ma5", False))
        daily_ma5_gap = float(daily_ctx.get("daily_ma5_gap", 0.0) or 0.0)
        daily_ma5_state = str(daily_ctx.get("daily_ma5_state", "unknown") or "unknown")
        daily_ma10 = float(daily_ctx.get("daily_ma10", 0.0) or 0.0)
        daily_ma20 = float(daily_ctx.get("daily_ma20", 0.0) or 0.0)
        daily_ma30 = float(daily_ctx.get("daily_ma30", 0.0) or 0.0)
        daily_ma60 = float(daily_ctx.get("daily_ma60", 0.0) or 0.0)
        daily_ma120 = float(daily_ctx.get("daily_ma120", 0.0) or 0.0)
        daily_ma150 = float(daily_ctx.get("daily_ma150", 0.0) or 0.0)
        daily_ma180 = float(daily_ctx.get("daily_ma180", 0.0) or 0.0)
        daily_ma250 = float(daily_ctx.get("daily_ma250", 0.0) or 0.0)
        daily_ma365 = float(daily_ctx.get("daily_ma365", 0.0) or 0.0)
        daily_buy_t_ok = daily_status == "ok" and daily_ma5 > 0 and daily_ma5_state in {"near_ma5_chop", "above_ma5_trend"}
        daily_buy_t_relaxed = daily_buy_t_ok and daily_ma5_state == "above_ma5_trend"
        daily_sell_t_preferred = daily_ma5_state == "below_ma5_weak"
        daily_support_gap = float(daily_ctx.get("daily_support_gap", 0.0) or 0.0)
        daily_breakdown_risk = bool(daily_ctx.get("daily_breakdown_risk", False))
        daily_hard_breakdown = bool(daily_ctx.get("daily_hard_breakdown", False))
        daily_overheated = bool(daily_ctx.get("daily_overheated", False))
        daily_pullback_support = bool(daily_ctx.get("daily_pullback_support", False))
        daily_near_support = bool(daily_ctx.get("daily_near_support", False))
        # V1.8fix: 当日线数据不可用时，用 VWAP 偏离度作为替代确认，避免硬阻断所有低吸
        # V1.17修正: 当5分钟量能反转信号确认时，直接允许买入（无需日线确认）
        if not daily_buy_t_ok and daily_status != "ok":
            vwap_deviation = (vwap - price) / vwap if vwap else 0.0
            if vwap_deviation > 0.005 and mom5 > -0.005 and not daily_hard_breakdown:
                daily_buy_t_ok = True  # 降级：价格低于 VWAP 0.5% 以上且未加速下跌，允许低吸
                daily_buy_t_relaxed = daily_buy_t_ok and daily_ma5_state == "above_ma5_trend"
            elif is_volume_reversal:
                # V1.17: 缩量止跌+放量反攻是强低吸信号，无需日线确认
                daily_buy_t_ok = True
                daily_buy_t_relaxed = False
        indicators = {
            "price": price,
            "rsi": rsi,
            "bb_pct": bb_pct,
            "vwap": vwap,
            "ema_spread": ema_spread,
            "range_pos": range_pos,
            "market_state": market_state,
            "benchmark_code": benchmark.get("benchmark_code", ""),
            "benchmark_name": benchmark.get("benchmark_name", ""),
            "benchmark_state": benchmark_state,
            "benchmark_gate": benchmark_gate,
            "benchmark_reason": benchmark_reason,
            "daily_status": daily_ctx.get("daily_status", "unknown"),
            "daily_gate": daily_ctx.get("daily_gate", "neutral"),
            "daily_trend_bg": daily_ctx.get("daily_trend_bg", "unknown"),
            "daily_ma5": daily_ctx.get("daily_ma5", 0.0),
            "daily_ma5_slope": daily_ctx.get("daily_ma5_slope", 0.0),
            "daily_above_ma5": daily_ctx.get("daily_above_ma5", False),
            "daily_ma5_gap": daily_ctx.get("daily_ma5_gap", 0.0),
            "daily_ma5_state": daily_ctx.get("daily_ma5_state", "unknown"),
            "daily_buy_t_ok": daily_buy_t_ok,
            "daily_sell_t_preferred": daily_sell_t_preferred,
            "daily_buy_t_relaxed": daily_buy_t_relaxed,
            "daily_ma10": daily_ctx.get("daily_ma10", 0.0),
            "daily_ma20": daily_ctx.get("daily_ma20", 0.0),
            "daily_ma30": daily_ctx.get("daily_ma30", 0.0),
            "daily_ma60": daily_ctx.get("daily_ma60", 0.0),
            "daily_ma120": daily_ctx.get("daily_ma120", 0.0),
            "daily_ma150": daily_ctx.get("daily_ma150", 0.0),
            "daily_ma180": daily_ctx.get("daily_ma180", 0.0),
            "daily_ma250": daily_ctx.get("daily_ma250", 0.0),
            "daily_ma365": daily_ctx.get("daily_ma365", 0.0),
            "daily_support_name": daily_ctx.get("daily_support_name", ""),
            "daily_support_level": daily_ctx.get("daily_support_level", 0.0),
            "daily_support_gap": daily_ctx.get("daily_support_gap", 0.0),
            "daily_breakdown_risk": daily_ctx.get("daily_breakdown_risk", False),
            "daily_hard_breakdown": daily_ctx.get("daily_hard_breakdown", False),
            "daily_overheated": daily_ctx.get("daily_overheated", False),
            "profit_guard_active": False,
            # V1.13: 15分钟线低吸指标
            "rsi_15m": rsi_15m,
            "macd_hist_15m": macd_hist_15m,
            "ema_spread_15m": ema_spread_15m,
            "vol_ratio_15m": vol_ratio_15m,
            "kinetic_exhaustion_15m": is_kinetic_exhaustion,
            "near_15m_support": is_near_15m_support,
            "multi_bottom_15m": is_multi_bottom_15m,
            "support_level_15m": support_level_15m,
            "pressure_support": diag.get("pressure_support", {}),
        }

        # ==================== V1.19: 弱势震荡/45度斜率/均线穿越检测 ====================
        # 基于用户7月7日反馈：区分"均线上下窜可做T" vs "全天均线下不可低吸"
        # V1.19: 检测窗口从90分钟扩展到120分钟，更准确地识别长期趋势
        # 增加穿越噪声过滤（0.15%），避免价格贴VWAP时的虚假穿越
        is_weak_oscillation = False
        is_steep_decline = False
        is_vwap_crossing = False
        vwap_cross_count = 0
        price_below_vwap_ratio = 0.0
        slope_pct_per_min = 0.0
        
        if len(df) >= 120:
            recent_df = df.iloc[-120:].copy()
            prices = recent_df["close"].astype(float).values
            vwaps = recent_df["vwap"].astype(float).values
            
            # 1. 弱势震荡：价格低于VWAP的比例
            below_vwap_count = sum(1 for p, v in zip(prices, vwaps) if v > 0 and p < v)
            price_below_vwap_ratio = below_vwap_count / len(prices) if len(prices) > 0 else 0.0
            
            # 2. 均线穿越检测：价格穿越VWAP的次数（带噪声过滤0.3%）
            cross_noise_pct = 0.003
            for i in range(1, len(prices)):
                prev_above = vwaps[i-1] > 0 and prices[i-1] >= vwaps[i-1] * (1 + cross_noise_pct)
                curr_above = vwaps[i] > 0 and prices[i] >= vwaps[i] * (1 + cross_noise_pct)
                if prev_above != curr_above:
                    # 额外确认穿越幅度（至少0.3%）
                    cross_dist = abs(prices[i] - vwaps[i]) / vwaps[i] if vwaps[i] > 0 else 0
                    if cross_dist >= 0.003:
                        vwap_cross_count += 1
            
            # 3. 45度斜率检测：线性回归（120分钟窗口）
            x = np.arange(len(prices))
            if len(prices) >= 5 and np.std(prices) > 0.001:
                slope, intercept = np.polyfit(x, prices, 1)
                # 斜率转换为每120分钟百分比
                mean_price = np.mean(prices)
                slope_pct_per_min = (slope * len(prices)) / mean_price * 100 if mean_price > 0 else 0
                is_steep_decline = slope_pct_per_min < -0.12  # 120分钟跌超0.12%视为45度下降
            
            # 4. 弱势震荡判定：平开或低开 + 80%时间在均线下 + 今日涨幅<1%
            is_weak_open = abs(open_gap) <= 0.005 or open_gap < 0
            is_weak_oscillation = is_weak_open and price_below_vwap_ratio > 0.80 and today_ret < 0.01
            
            # 5. 均线上下窜判定：穿越VWAP >= 2次（120分钟窗口）
            is_vwap_crossing = vwap_cross_count >= 2
        
        indicators["v1_18_weak_oscillation"] = is_weak_oscillation
        indicators["v1_18_steep_decline"] = is_steep_decline
        indicators["v1_18_vwap_crossing"] = is_vwap_crossing
        indicators["v1_18_vwap_cross_count"] = vwap_cross_count
        indicators["v1_18_below_vwap_ratio"] = round(price_below_vwap_ratio, 2)
        indicators["v1_18_slope_pct"] = round(slope_pct_per_min, 3)
        
        # 同时保存到 diagnostics 供调试
        diag["v1_18_weak_oscillation"] = is_weak_oscillation
        diag["v1_18_steep_decline"] = is_steep_decline
        diag["v1_18_vwap_crossing"] = is_vwap_crossing
        diag["v1_18_below_vwap_ratio"] = round(price_below_vwap_ratio, 2)
        diag["v1_18_slope_pct"] = round(slope_pct_per_min, 3)
        
        # V1.14: 多维度支撑位识别 — 开盘急跌时判断是否为"砸到支撑"而非"破位"
        support_levels = []  # [(name, level, gap_pct), ...]
        # 1. 昨日最低点
        prev_day_low = float(indicators.get("prev_low", 0)) or float(indicators.get("daily_prev_low", 0))
        if prev_day_low <= 0:
            # 从 df 中推断昨日最低（如果 df 中有跨日数据）
            if "date" in df.columns and len(df) > 1:
                dates = df["date"].unique()
                if len(dates) >= 2:
                    prev_day = dates[-2]
                    prev_day_df = df[df["date"] == prev_day]
                    if not prev_day_df.empty:
                        prev_day_low = float(prev_day_df["low"].min())
        # 2. 最近5日最低（从 indicators 中的日线上下文）
        recent_5d_low = float(daily_ctx.get("daily_ma10", 0)) * 0.95  # 简化近似
        # 3. 日线布林下轨（如果有）
        daily_bb_lower = float(daily_ctx.get("daily_bb_lower", 0))
        # 4. 日线MA20
        daily_ma20 = float(daily_ctx.get("daily_ma20", 0))
        # 5. 日线MA30
        daily_ma30 = float(daily_ctx.get("daily_ma30", 0))
        # 6. 15分钟线低点（已有 support_level_15m）
        
        # 收集所有有效支撑位
        for support_name, level in [
            ("昨日低点", prev_day_low),
            ("MA20", daily_ma20),
            ("MA30", daily_ma30),
            ("15分低点", support_level_15m),
        ]:
            if level > 0 and price > 0:
                gap_pct = abs(price - level) / level
                if gap_pct < 0.01:  # 在1%范围内视为"触及支撑"
                    support_levels.append((support_name, level, gap_pct))
        
        # 排序：gap_pct 最小的优先
        support_levels.sort(key=lambda x: x[2])
        nearest_support = support_levels[0] if support_levels else None
        is_near_any_support = nearest_support is not None
        
        # 记录到 indicators 供后续使用
        indicators["nearest_support_name"] = nearest_support[0] if nearest_support else ""
        indicators["nearest_support_level"] = nearest_support[1] if nearest_support else 0.0
        indicators["nearest_support_gap"] = nearest_support[2] if nearest_support else 999.0
        indicators["is_near_any_support"] = is_near_any_support
        indicators["support_levels_count"] = len(support_levels)
        
        # 开盘急跌旁路判断：开盘后5分钟内，跌幅>2%，且触及支撑位
        is_open_dip_support = False
        open_dip_reason = ""
        if 930 <= t_val <= 935 and today_ret < -0.02 and is_near_any_support:
            is_open_dip_support = True
            open_dip_reason = f"开盘后急跌{today_ret*100:.1f}%，触及{nearest_support[0]}({nearest_support[1]:.2f})"
        indicators["is_open_dip_support"] = is_open_dip_support
        indicators["open_dip_reason"] = open_dip_reason

        sell_details, buy_details = [], []
        # V1.11: 早盘冲高窗口优化 - 09:30-09:35为机会窗口(+8)，09:36-09:45为观察期(0)
        if 1000 <= t_val <= 1045 or 1400 <= t_val <= 1445:
            time_score = 15
        elif 930 <= t_val <= 935:
            time_score = 8
        elif 936 <= t_val <= 945:
            time_score = 0
        else:
            time_score = 0
        sell_score = buy_score = time_score
        required_profit_buy = p["min_profit_space"] * 1.5 if rsi < 15 else p["min_profit_space"]
        buy_profit_space = (vwap - price) / price if price > 0 else 0.0
        if buy_profit_space > 0:
            buy_score += 12
            buy_details.append({"指标": "回归空间", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "现价低于均价", "加分": 12})
        if buy_profit_space > required_profit_buy:
            buy_score += 15
            buy_details.append({"指标": "盈利空间", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "距离均价回归空间足", "加分": 15})
        # V1.8fix: 新增 VWAP 深度偏离加分，直接奖励充分低吸
        if buy_profit_space > 0.01:
            buy_score += 5
            buy_details.append({"指标": "VWAP深度偏离", "当前": f"+{buy_profit_space*100:.2f}%", "解读": "价格低于均价1%以上，深度低吸", "加分": 5})
        # V1.11: 下午回落接回因子 - 13:00-14:30回落到VWAP下方且RSI超卖
        had_afternoon_pullback = False
        if 1300 <= t_val <= 1430 and buy_profit_space > 0.005 and rsi <= p["rsi_oversold"]:
            buy_score += 8
            buy_details.append({"指标": "下午回落", "当前": f"RSI{rsi:.1f}/低于VWAP{buy_profit_space*100:.2f}%", "解读": "下午回落超卖，建议接回", "加分": 8})
            had_afternoon_pullback = True
        if rsi <= p["rsi_oversold"]:
            buy_score += 12
            buy_details.append({"指标": "RSI超卖", "当前": f"{rsi:.1f}", "阈值": f"≤{PARAMS['rsi_oversold']}", "加分": 12})
        if bb_pct <= 0.15:
            buy_score += 8
            buy_details.append({"指标": "布林偏下", "当前": f"{bb_pct:.2f}", "阈值": "≤0.15", "加分": 8})
        if buy_profit_space > 0 and rsi <= p["rsi_oversold"] and mom5 < 0:
            buy_score -= 4
            buy_details.append({"指标": "回落未确认", "当前": f"{mom5*100:.2f}%", "阈值": "5分钟仍未转正", "加分": -4})
        if buy_profit_space > 0 and range_pos > 0.35:
            buy_score -= 2
            buy_details.append({"指标": "低位不够深", "当前": f"{range_pos:.2f}", "阈值": "≤0.35", "加分": -2})
        if macd_hist > prev_macd_hist and macd_hist < 0:
            buy_score += 15
            buy_details.append({"指标": "MACD拐头", "当前": f"{macd_hist:.4f}", "阈值": "负区抬头", "加分": 15})
            if abs(macd_hist) > p["macd_strong_threshold"]:
                buy_score += p["macd_strong_boost"]
                buy_details.append({"指标": "MACD强拐头", "当前": f"{macd_hist:.4f}", "阈值": f">{PARAMS['macd_strong_threshold']}", "加分": PARAMS["macd_strong_boost"]})
        if vol_ratio >= p["vol_ratio_confirm"]:
            buy_score += p["vol_confirm_boost"]
            buy_details.append({"指标": "量能确认", "当前": f"{vol_ratio:.2f}", "阈值": f"≥{PARAMS['vol_ratio_confirm']}", "加分": PARAMS["vol_confirm_boost"]})
        if lower_shadow >= 0.35:
            buy_score += 8
            buy_details.append({"指标": "长下影", "当前": f"{lower_shadow:.2f}", "阈值": "≥0.35", "加分": 8})
        if ema_spread > prev_ema_spread and ema_spread > -0.002:
            buy_score += 4
            buy_details.append({"指标": "EMA转强", "当前": f"{ema_spread*100:.2f}%", "阈值": "短均线改善", "加分": 4})
        if buy_score < p["buy_confirm_min_score"] and len(buy_details) >= p["buy_confirm_min_factors"]:
            buy_score -= 2
            buy_details.append({"指标": "买点未成型", "当前": f"{buy_score:.0f}", "阈值": f"≥{PARAMS['buy_confirm_min_score']}且确认因子不足", "加分": -2})
        if buy_score >= p["buy_confirm_min_score"] and mom5 <= 0 and price < vwap and range_pos <= 0.45:
            buy_score += 6
            buy_details.append({"指标": "回落确认", "当前": f"{mom5*100:.2f}%", "阈值": "贴近VWAP且5分钟不再走弱", "加分": 6})
        elif buy_score >= p["buy_confirm_min_score"] and mom5 > 0 and price < vwap:
            buy_score -= 4
            buy_details.append({"指标": "反弹过快", "当前": f"{mom5*100:.2f}%", "阈值": "仍需低位回落确认", "加分": -4})
        if buy_score >= p["buy_confirm_min_score"] and price > vwap and mom5 > 0:
            buy_score -= 3
            buy_details.append({"指标": "买点过热", "当前": f"{price:.2f}", "阈值": "确认买点不应强行追高", "加分": -3})
        if range_pos <= p["range_pos_low_threshold"] and mom5 > -0.01:
            buy_score += 4
            buy_details.append({"指标": "区间低位", "当前": f"{range_pos:.2f}", "阈值": f"≤{PARAMS['range_pos_low_threshold']}", "加分": 4})
        if daily_pullback_support and price <= vwap and mom5 > -0.004:
            buy_score += 8
            buy_details.append({"指标": "日线回踩承接", "当前": f"{price:.2f}/{vwap:.2f}", "阈值": "回踩支撑后止跌", "加分": 8})
        elif daily_near_support and price <= vwap and mom5 > -0.002:
            buy_score += 4
            buy_details.append({"指标": "日线支撑企稳", "当前": f"{price:.2f}/{vwap:.2f}", "阈值": "支撑附近不再走弱", "加分": 4})
        # V1.15: 均线支撑确认 — 冲高回落后站稳短期均线，理想低吸点
        # 案例：摩恩电气 7.2 冲高回落→尾盘站稳MA5→全天低点→理想低吸
        ma_support = None
        ma_support_boost = 0
        if daily_ma5 > 0 and price >= daily_ma5 * 0.99 and daily_ma5_slope > 0:
            # 冲高回落检测：今日高点与当前价差 > 1%
            today_high = float(df["high"].max()) if not df.empty else price
            had_pullback = today_high > price * 1.01
            if price <= vwap and had_pullback:
                ma_support_boost = 12
                buy_details.append({
                    "指标": "MA5支撑确认",
                    "当前": f"{price:.2f} vs MA5:{daily_ma5:.2f}（斜率+{daily_ma5_slope*100:.2f}%）",
                    "解读": f"冲高回落后站稳MA5（今日高{today_high:.2f}→现{price:.2f}），均线向上支撑有效，理想低吸点",
                    "加分": ma_support_boost,
                })
                ma_support = {"name": "MA5", "level": daily_ma5, "type": "support_confirmed", "pullback": had_pullback}
        elif daily_ma10 > 0 and price >= daily_ma10 * 0.99 and daily_ma10 > daily_ma5 and price < daily_ma5 * 0.995:
            # 价格跌破MA5但仍在MA10上方，MA10提供更强支撑
            today_high = float(df["high"].max()) if not df.empty else price
            had_pullback = today_high > price * 1.01
            if price <= vwap and had_pullback:
                ma_support_boost = 10
                buy_details.append({
                    "指标": "MA10支撑确认",
                    "当前": f"{price:.2f} vs MA10:{daily_ma10:.2f}",
                    "解读": f"跌破MA5后站稳MA10（今日高{today_high:.2f}→现{price:.2f}），次强支撑有效，低吸机会",
                    "加分": ma_support_boost,
                })
                ma_support = {"name": "MA10", "level": daily_ma10, "type": "support_confirmed", "pullback": had_pullback}
        buy_score += ma_support_boost
        indicators["ma_support"] = ma_support

        if is_strong_pullback:
            buy_score += 30
            buy_details.append({"指标": "主升浪回踩", "当前": "贴近VWAP", "解读": "强势突破股回踩均价", "加分": 30})
        elif is_strong_trend and price >= prev_high and vol_ratio >= p["vol_ratio_confirm"] and benchmark_gate != "weak":
            buy_score += 20
            buy_details.append({"指标": "强势突破", "当前": f"{price:.2f}", "解读": "突破前高并放量，提示顺势加仓", "加分": 20})

        # V1.14fix: buy_threshold 提前定义，避免第670行支撑位加分时未绑定
        buy_threshold = self._dynamic_threshold("buy", price, vwap, rsi, vol_ratio, holding, market_state, is_strong_pullback, code)
        sell_threshold = self._dynamic_threshold("sell", price, vwap, rsi, vol_ratio, holding, market_state, is_strong_pullback, code)

        # V1.13: 15分钟线低吸因子 — 下跌动能衰竭 + 强支撑确认
        if not df_15min.empty and len(df_15min) >= PARAMS.get("min_15min_bars", 3):
            if is_kinetic_exhaustion:
                buy_score += PARAMS.get("kinetic_exhaustion_boost", 10)
                buy_details.append({"指标": "15分动能衰竭", "当前": f"MACD{macd_hist_15m:.4f}↑", "解读": "15分钟下跌动能衰竭，低吸窗口", "加分": PARAMS.get("kinetic_exhaustion_boost", 10)})
            if is_near_15m_support:
                buy_score += PARAMS.get("support_15m_boost", 8)
                buy_details.append({"指标": "15分强支撑", "当前": f"{price:.2f}≈{support_level_15m:.2f}", "解读": "15分钟级别强支撑附近", "加分": PARAMS.get("support_15m_boost", 8)})
            if is_multi_bottom_15m:
                buy_score += PARAMS.get("multi_bottom_15m_boost", 6)
                buy_details.append({"指标": "15分多重底", "当前": "多次测试支撑", "解读": "15分钟级别形成多重底结构", "加分": PARAMS.get("multi_bottom_15m_boost", 6)})
            if rsi_15m <= PARAMS.get("rsi_15m_oversold", 35):
                buy_score += 5
                buy_details.append({"指标": "15分RSI超卖", "当前": f"{rsi_15m:.1f}", "阈值": f"≤{PARAMS.get('rsi_15m_oversold', 35)}", "加分": 5})
            if prev_ema_spread_15m != 0.0 and ema_spread_15m > prev_ema_spread_15m and ema_spread_15m > -0.002:
                buy_score += PARAMS.get("ema_15m_improve_boost", 3)
                buy_details.append({"指标": "15分EMA改善", "当前": f"{ema_spread_15m*100:.2f}%", "解读": "15分钟短周期趋势改善", "加分": PARAMS.get("ema_15m_improve_boost", 3)})
        
        # V1.17: 5分钟线缩量止跌+放量反攻 — 基于7月6日反馈
        # V1.22修正: 只有大阳线反包确认时才给予高加分（防止开盘急跌时半山腰买入）
        if not df_5min.empty and len(df_5min) >= 5:
            if is_strong_bullish_reversal:
                boost = PARAMS.get("volume_reversal_boost", 18)
                buy_score += boost
                buy_details.append({
                    "指标": "5分大阳线反包",
                    "当前": f"涨幅{indicators.get('bullish_reversal_pct', 0)*100:.1f}%/实体占比{indicators.get('bullish_reversal_body', 0)*100:.0f}%",
                    "解读": "5分钟放量下跌后出现大阳线反包，资金确认抄底，低吸窗口（7月8日反馈）",
                    "加分": boost,
                })
                # 大阳线反包信号出现时，适当降低买入阈值
                buy_threshold -= 15
                buy_details.append({"指标": "反包门槛放宽", "当前": f"阈值{buy_threshold:.0f}", "解读": "5分钟大阳线反包确认，降低买入门槛", "加分": 0})
            elif is_volume_reversal:
                # 小阳线/十字星反转：仅给予小加分，不放宽门槛
                buy_score += 8
                buy_details.append({
                    "指标": "5分弱企稳",
                    "当前": f"前4阴{vr_bearish_count}根/高点{'↓' if vr_high_declining else '震荡'}",
                    "解读": "5分钟级别弱企稳，但无大阳线反包确认，谨慎对待",
                    "加分": 8,
                })

        # V1.26: 低点抬高支撑确认加分 — 华工科技 07-14 反馈
        # 从高点下跌>4%后，低点抬高说明承接有力，并非单边下跌，给予买入加分
        if higher_low_detected and higher_low_detail:
            buy_score += 15
            buy_details.append({
                "指标": "低点抬高支撑",
                "当前": f"高{higher_low_detail.get('day_high', 0):.2f}→跌{higher_low_detail.get('drop_from_high', 0)*100:.1f}%→低抬高+{higher_low_detail.get('low_raise_pct', 0)*100:.2f}%",
                "解读": "从高点下跌后低点连续抬高，承接有力，非单边下跌，低吸机会（华工科技07-14反馈）",
                "加分": 15,
            })
            # 低点抬高支撑确认时，降低买入阈值
            buy_threshold -= 8
            buy_details.append({"指标": "支撑门槛放宽", "当前": f"阈值{buy_threshold:.0f}", "解读": "低点抬高支撑确认，降低买入门槛", "加分": 0})

        # V1.14: 多维度支撑位加分 — 开盘急跌触及支撑时，降低买入门槛
        if is_near_any_support and nearest_support:
            support_name, support_level, support_gap = nearest_support
            # 越接近支撑，加分越多
            if support_gap < 0.003:  # 距离<0.3%
                boost = 18
                buy_details.append({"指标": f"{support_name}强支撑", "当前": f"{price:.2f}≈{support_level:.2f}", "解读": f"价格触及{support_name}强支撑，急跌即低吸机会", "加分": boost})
            elif support_gap < 0.005:  # 距离<0.5%
                boost = 12
                buy_details.append({"指标": f"{support_name}支撑", "当前": f"{price:.2f}≈{support_level:.2f}", "解读": f"价格接近{support_name}支撑，关注低吸机会", "加分": boost})
            elif support_gap < 0.01:  # 距离<1%
                boost = 6
                buy_details.append({"指标": f"{support_name}附近", "当前": f"{price:.2f}≈{support_level:.2f}", "解读": f"价格靠近{support_name}", "加分": boost})
            buy_score += boost
            # 当价格触及支撑时，降低买入阈值（放宽门槛）
            if support_gap < 0.005:
                buy_threshold -= 5
                buy_details.append({"指标": "支撑门槛放宽", "当前": f"-{buy_threshold:.0f}", "解读": "触及支撑，降低买入门槛", "加分": 0})
            
        # ==================== V1.15fix: 场景化买入因子（用户反馈驱动）====================
        # 计算当前索引位置（用于历史数据回溯）
        current_idx_b = len(df) - 1
        
        # 1. EOD_VWAP_BUY: 尾盘跌破均线买入（6月5日/8日/11日案例）
        if 1430 <= t_val <= 1500 and price < vwap * 0.998 and mom5 < 0 and buy_profit_space > 0:
            buy_score += 10
            buy_details.append({"指标": "尾盘跌破均线", "当前": f"{price:.2f}<{vwap*0.998:.2f}", "解读": "尾盘跌破均价线，VWAP下方低吸机会", "加分": 10})
        
        # 2. EOD_SURGE_BUY: 尾盘拉升买入（6月12日案例：14:28尾盘拉升）
        if 1430 <= t_val <= 1500 and price > vwap * 1.005 and mom5 > 0.003 and buy_profit_space > 0:
            buy_score += 8
            buy_details.append({"指标": "尾盘拉升", "当前": f"{price:.2f}>{vwap*1.005:.2f}", "解读": "尾盘放量拉升，强势信号", "加分": 8})
        
        # 3. MA_BOUNCE_BUY: 均线回踩支撑买入（6月9日案例：13:10回踩均线）
        if vwap > 0 and price <= vwap * 1.005 and price >= vwap * 0.99 and mom5 < 0 and mom5 > -0.005 and buy_profit_space > 0:
            buy_score += 10
            buy_details.append({"指标": "均线回踩", "当前": f"{price:.2f}≈{vwap:.2f}", "解读": "回踩均价线未跌破，支撑有效，低吸", "加分": 10})
        
        # 4. VWAP_BREAK_PULLBACK: 均线突破后回踩买入（6月22日案例：13:29均线突破回踩）
        vwap_broken = False
        if current_idx_b >= 10 and vwap > 0:
            recent_highs = df.iloc[max(0, current_idx_b - 60):current_idx_b]["high"].astype(float)
            if len(recent_highs) > 0:
                vwap_broken = any(h > vwap * 1.005 for h in recent_highs)
        if vwap_broken and price <= vwap * 1.005 and price >= vwap * 0.99 and mom5 > -0.005 and buy_profit_space > 0:
            buy_score += 12
            buy_details.append({"指标": "均线上破回踩", "当前": f"{price:.2f}≈{vwap:.2f}", "解读": "曾突破VWAP后回踩，低吸确认", "加分": 12})
        
        # V1.22: 开盘急跌惩罚 — 前15分钟内，5分钟线无大阳线反包，大幅惩罚买入（防止买在半山腰）
        if current_idx_b <= p.get("open_dip_max_mins", 15) and not is_strong_bullish_reversal:
            # 如果5分钟线继续下跌（mom2_5m < 0），大幅惩罚
            if mom2_5m < -0.005:
                penalty = p.get("open_dip_buy_penalty", 25)
                buy_score -= penalty
                buy_details.append({
                    "指标": "开盘急跌无反包",
                    "当前": f"开盘{current_idx_b}分钟/5分跌{mom2_5m*100:.1f}%",
                    "解读": "开盘急跌中无大阳线反包确认，禁止半山腰抄底（7月8日反馈）",
                    "加分": -penalty,
                })
                # 大幅提高买入阈值
                buy_threshold += 10
        
        # 开盘急跌旁路：开盘后5分钟内跌幅>2%且触及支撑，直接给出旁路建议
        # 这个旁路在后面的买入确认逻辑中处理

        required_profit_sell = p["min_profit_space"] * 1.5 if rsi > 85 else p["min_profit_space"]
        sell_profit_space = (price - vwap) / vwap if vwap else 0.0
        if sell_profit_space > 0:
            sell_score += 15
            sell_details.append({"指标": "回吐空间", "当前": f"+{sell_profit_space*100:.2f}%", "解读": "现价高于均价", "加分": 15})
        # V1.11: 早盘冲高因子 - 开盘后5分钟内快速拉升，优先高抛
        had_morning_surge = False
        if 930 <= t_val <= 935 and today_ret > 0.006 and price > vwap * 1.005:
            surge_strength = min(18, int(today_ret * 1000))
            sell_score += surge_strength
            sell_details.append({"指标": "早盘冲高", "当前": f"+{today_ret*100:.2f}%", "解读": "开盘后急速拉升，建议高抛做T", "加分": surge_strength})
            had_morning_surge = True
        if sell_profit_space > required_profit_sell:
            sell_score += 15
            sell_details.append({"指标": "盈利空间", "当前": f"+{sell_profit_space*100:.2f}%", "解读": "覆盖手续费并获利", "加分": 15})
        if not is_strong_trend:
            if rsi >= p["rsi_overbought"]:
                sell_score += 15
                sell_details.append({"指标": "RSI超买", "当前": f"{rsi:.1f}", "阈值": f"≥{PARAMS['rsi_overbought']}", "加分": 15})
            if bb_pct >= 0.85:
                sell_score += 12
                sell_details.append({"指标": "布林偏上", "当前": f"{bb_pct:.2f}", "阈值": "≥0.85", "加分": 12})
        if macd_hist < prev_macd_hist and macd_hist > 0:
            sell_score += 10
            sell_details.append({"指标": "MACD拐头", "当前": f"{macd_hist:.4f}", "阈值": "正区走弱", "加分": 10})
        if vol_ratio >= p["vol_ratio_confirm"]:
            sell_score += p["vol_confirm_boost"]
            sell_details.append({"指标": "量能确认", "当前": f"{vol_ratio:.2f}", "阈值": f"≥{PARAMS['vol_ratio_confirm']}", "加分": PARAMS["vol_confirm_boost"]})
        if upper_shadow >= 0.5:
            sell_score += 15
            sell_details.append({"指标": "长上影", "当前": f"{upper_shadow:.2f}", "阈值": "≥0.5", "加分": 15})
        if ema_spread < prev_ema_spread and ema_spread < 0.002:
            sell_score += 4
            sell_details.append({"指标": "EMA转弱", "当前": f"{ema_spread*100:.2f}%", "阈值": "短均线走弱", "加分": 4})
        if range_pos >= p["range_pos_high_threshold"] and mom5 < 0.01:
            sell_score += 4
            sell_details.append({"指标": "区间高位", "当前": f"{range_pos:.2f}", "阈值": f"≥{PARAMS['range_pos_high_threshold']}", "加分": 4})

        holding_start = self.t_cycle_start_time.get(code)
        holding_minutes = (_now() - holding_start).total_seconds() / 60 if holding_start else 0.0
        if holding_minutes >= p["sell_holding_min_minutes"]:
            bonus = p["sell_score_boost_holding"] if holding_minutes < p["sell_holding_strict_minutes"] else p["sell_score_boost_holding"] + 2
            sell_score += bonus
            sell_details.append({"指标": "持仓时间", "当前": f"{holding_minutes:.0f}分钟", "阈值": f"≥{PARAMS['sell_holding_min_minutes']}分钟", "加分": bonus})
            if holding_minutes < p["sell_holding_strict_minutes"] and sell_score - buy_score < 8:
                sell_score -= 4
                sell_details.append({"指标": "时间未成熟", "当前": f"{holding_minutes:.0f}分钟", "阈值": f"≥{PARAMS['sell_holding_strict_minutes']}分钟或卖优更强", "加分": -4})
        if 1455 <= t_val <= 1500 and sell_score >= 50 and sell_score - buy_score >= 8:
            sell_score += p["sell_score_boost_eod"]
            sell_details.append({"指标": "收盘前", "当前": f"{t_val}", "阈值": "14:55-15:00 且卖分足够", "加分": PARAMS["sell_score_boost_eod"]})
        if holding_minutes >= p["sell_holding_strict_minutes"] and sell_score - buy_score >= 6:
            sell_score += p["sell_momentum_bonus"]
            sell_details.append({"指标": "持仓转弱", "当前": f"{holding_minutes:.0f}分钟", "阈值": f"≥{PARAMS['sell_holding_strict_minutes']}分钟且卖优于买", "加分": PARAMS["sell_momentum_bonus"]})

        # ==================== V1.20: 场景化卖出因子（历史累积判断 + 观察确认锁定）====================
        # 辅助指标计算
        current_idx = len(df) - 1
        minutes_since_open = current_idx
        open_low = float(df["low"].iloc[:min(15, len(df))].min()) if len(df) > 0 else today_open
        recent_10 = df.iloc[max(0, current_idx - 10):current_idx + 1]
        recent_high_10 = float(recent_10["high"].max()) if not recent_10.empty else price
        pb_10 = (recent_high_10 - price) / recent_high_10 if recent_high_10 > 0 else 0
        if current_idx >= 3:
            rate3 = (price - float(df.iloc[current_idx - 3]["close"])) / float(df.iloc[current_idx - 3]["close"]) if float(df.iloc[current_idx - 3]["close"]) > 0 else 0
        else:
            rate3 = 0
        if current_idx >= 6:
            prev_rate3 = (float(df.iloc[current_idx - 3]["close"]) - float(df.iloc[current_idx - 6]["close"])) / float(df.iloc[current_idx - 6]["close"]) if float(df.iloc[current_idx - 6]["close"]) > 0 else 0
        else:
            prev_rate3 = 0
        day_high_so_far = float(df["high"].iloc[:current_idx + 1].max()) if len(df) > 0 else price
        limit_up_triggered = any(float(r["high"]) >= pre_close * 1.099 for _, r in df.iterrows()) if pre_close > 0 else False
        
        # ==================== V1.24: 压力位/支撑位锚定卖出逻辑 ====================
        # 当价格接近压力位且动量衰竭时，根据压力重要性决定卖出比例
        ps = diag.get("pressure_support", {})
        pressure_name = ps.get("pressure_name", "")
        pressure_level = ps.get("pressure_level", 0.0)
        pressure_gap = ps.get("pressure_gap", 0.0)
        is_major_pressure = ps.get("is_major_pressure", False)
        sell_qty_pct = ps.get("sell_qty_pct", 100)
        if pressure_level > 0 and price > 0:
            # 价格接近压力位（差距<0.5%）且动量放缓
            near_pressure = 0 < pressure_gap < 0.005
            # 价格曾经突破压力位但回落（假突破）
            fake_breakout = day_high_so_far > pressure_level and price < pressure_level * 0.995
            if near_pressure or fake_breakout:
                if is_major_pressure:
                    # 重要压力: 全部卖出信号加强
                    sell_score += 20
                    sell_details.append({
                        "指标": f"重要压力受阻({pressure_name})",
                        "当前": f"价{price:.2f}≈压{pressure_level:.2f}(差{pressure_gap*100:.2f}%)",
                        "解读": f"冲击{pressure_name}压力位失败，重要压力，建议全部卖出",
                        "加分": 20,
                    })
                else:
                    # 短期压力: 部分卖出信号
                    sell_score += 12
                    sell_details.append({
                        "指标": f"短期压力受阻({pressure_name})",
                        "当前": f"价{price:.2f}≈压{pressure_level:.2f}(差{pressure_gap*100:.2f}%)",
                        "解读": f"冲击{pressure_name}压力位受阻，短期压力，建议卖出一部分",
                        "加分": 12,
                    })
            # 价格跌破支撑位
            support_level = ps.get("support_level", 0.0)
            support_gap = ps.get("support_gap", 0.0)
            if support_level > 0 and price < support_level * 0.995 and support_gap < -0.005:
                sell_score += 15
                sell_details.append({
                    "指标": f"跌破支撑({ps.get('support_name','')})",
                    "当前": f"价{price:.2f}<支{support_level:.2f}(差{abs(support_gap)*100:.2f}%)",
                    "解读": "跌破关键支撑位，趋势转弱，建议卖出",
                    "加分": 15,
                })
        
        # ===== 振幅门控：振幅<3%时不触发场景化卖出（避免无效波动）=====
        amplitude_gate = day_amplitude >= 0.03
        
        # ===== 强承接检测：冲高后低点连续抬高 → 抑制卖出（6月18日案例）=====
        strong_support = False
        if current_idx >= 10:
            recent_10_lows = df.iloc[max(0, current_idx - 10):current_idx + 1]["low"].astype(float)
            if len(recent_10_lows) >= 5:
                # 检测最近5根低点是否连续抬高
                lows_5 = recent_10_lows.tail(5).values
                rising_lows = all(lows_5[i+1] > lows_5[i] for i in range(len(lows_5)-1))
                if rising_lows and price >= vwap * 0.995:
                    strong_support = True
        
        # V1.20: 强势震荡检测（华工科技0707案例：09:34-09:52在均线上方反复波动）
        strong_chop = self._is_strong_chop(df, current_idx, price, vwap)
        if strong_chop:
            diag["strong_chop_detected"] = True

        # V1.20: ETF早盘观察倍数
        etf_obs_mult = 2.0 if (is_etf and t_val < 1000) else 1.0

        # V1.26fix: 提前计算持仓盈亏，供所有场景化卖出因子使用（避免深套时误触发高抛）
        cost = float(holding.get("cost", 0) or 0)
        profit_pct = (price - cost) / cost if cost > 0 else 0
        is_deep_loss = cost > 0 and profit_pct < -0.05
        if is_deep_loss:
            diag["is_deep_loss"] = True
            diag["profit_pct"] = round(profit_pct * 100, 2)
        indicators["daily_loss_pct"] = round(profit_pct * 100, 2)

        # 1. GAP_BOUNCE20: 集合竞价弱势 + 早盘反弹高抛（保持，不受振幅门控限制，但加观察）
        # 条件：低开+早盘反弹+未突破开盘价
        gap_bounce_met = (minutes_since_open <= 15 and open_gap < -0.005 and
                          open_low > 0 and (price - open_low) / open_low > 0.003 and
                          price <= today_open * 1.005)
        gap_bounce_cancel = strong_chop or (price > today_open * 1.002)  # 反弹突破开盘价则取消
        gap_bounce_confirmed, gap_bounce_diag = self._check_scenario_factor(
            code, "GAP_BOUNCE20", gap_bounce_met, observation_minutes=2,
            lock_minutes=15, etf_observation_multiplier=etf_obs_mult,
            cancel_condition=gap_bounce_cancel
        )
        if gap_bounce_confirmed:
            if is_deep_loss:
                # V1.26fix: 深套时不应"高抛"，避免割肉误导
                sell_score -= 25
                sell_details.append({"指标": "低开反弹(深套抑制)", "当前": f"低{open_gap*100:.2f}%后回弹{(price-open_low)/open_low*100:.2f}%", "解读": f"深套{profit_pct*100:.1f}%：早盘反弹是减亏机会，但非做T高抛，抑制卖出防止割肉", "加分": -25})
            else:
                sell_score += 20
                sell_details.append({"指标": "低开反弹", "当前": f"低{open_gap*100:.2f}%后回弹{(price-open_low)/open_low*100:.2f}%", "解读": "集合竞价弱势，早盘反弹即高抛机会", "加分": 20})

        # 2. SPIKE30: 开盘冲高回落（V1.20: 需观察3分钟确认，强势震荡抑制）
        spike_met = (minutes_since_open <= 30 and day_high_so_far > today_open * 1.005
                     and amplitude_gate and pb_10 > 0.003)
        # 强势震荡时抑制；价格回升到近期高点的99.5%以上则取消观察
        spike_cancel = strong_chop or (price > day_high_so_far * 0.995)
        # ETF早盘：需确认滞涨（最近5分钟未创新高）
        if is_etf and t_val < 1000 and current_idx >= 5:
            recent_5_high = float(df.iloc[current_idx - 5:current_idx + 1]["high"].max())
            if recent_5_high == float(df.iloc[current_idx]["high"]):
                spike_met = False  # ETF仍在创新高，不进入观察
        spike_confirmed, spike_diag = self._check_scenario_factor(
            code, "SPIKE30", spike_met, observation_minutes=3,
            lock_minutes=30, etf_observation_multiplier=etf_obs_mult,
            cancel_condition=spike_cancel
        )
        if spike_confirmed:
            if is_deep_loss:
                sell_score -= 25
                sell_details.append({"指标": "开盘冲高回落(深套抑制)", "当前": f"高{day_high_so_far:.2f}→现{price:.2f}", "解读": f"深套{profit_pct*100:.1f}%：开盘冲高回落非做T高抛机会，抑制防止割肉", "加分": -25})
            else:
                sell_score += 20
                sell_details.append({"指标": "开盘冲高回落", "当前": f"高{day_high_so_far:.2f}→现{price:.2f}", "解读": "开盘后30分钟内冲高即回落，建议全部卖出", "加分": 20})

        # 3. FADE15: 冲顶动量衰竭（V1.20: 需观察2分钟确认）
        near_high = day_high_so_far > 0 and (day_high_so_far - price) / day_high_so_far < 0.003
        fade_met = (near_high and prev_rate3 > 0.003 and rate3 < prev_rate3 * 0.5 and amplitude_gate)
        # 动量重新加速则取消
        fade_cancel = (rate3 > prev_rate3 * 0.8) or strong_chop
        fade_confirmed, fade_diag = self._check_scenario_factor(
            code, "FADE15", fade_met, observation_minutes=2,
            lock_minutes=20, etf_observation_multiplier=etf_obs_mult,
            cancel_condition=fade_cancel
        )
        if fade_confirmed:
            if is_deep_loss:
                sell_score -= 20
                sell_details.append({"指标": "冲顶衰竭(深套抑制)", "当前": f"3分涨幅{rate3*100:.2f}%<{prev_rate3*100:.2f}%*0.5", "解读": f"深套{profit_pct*100:.1f}%：冲顶衰竭非做T高抛机会，抑制防止割肉", "加分": -20})
            else:
                sell_score += 15
                sell_details.append({"指标": "冲顶衰竭", "当前": f"3分涨幅{rate3*100:.2f}%<{prev_rate3*100:.2f}%*0.5", "解读": "接近日内新高但动量明显放缓，预判冲顶", "加分": 15})

        # 4. WEAK_REBOUND_SELL: 低开弱反弹避险（V1.20: 需观察2分钟确认）
        weak_reb_sell_met = (minutes_since_open <= 20 and open_gap < -0.005 and amplitude_gate
                             and today_open > 0 and (price - today_open) / today_open < 0.01
                             and price > today_open)
        # 反弹力度增强到1%以上则取消
        weak_reb_sell_cancel = (today_open > 0 and (price - today_open) / today_open >= 0.01) or strong_chop
        weak_reb_sell_confirmed, weak_reb_sell_diag = self._check_scenario_factor(
            code, "WEAK_REBOUND_SELL", weak_reb_sell_met, observation_minutes=2,
            lock_minutes=20, etf_observation_multiplier=etf_obs_mult,
            cancel_condition=weak_reb_sell_cancel
        )
        if weak_reb_sell_confirmed:
            if is_deep_loss:
                sell_score -= 20
                sell_details.append({"指标": "低开弱反弹(深套抑制)", "当前": f"低{open_gap*100:.2f}%后反弹{(price-today_open)/today_open*100:.2f}%<1%", "解读": f"深套{profit_pct*100:.1f}%：低开弱反弹非做T高抛机会，抑制防止割肉", "加分": -20})
            else:
                sell_score += 15
                sell_details.append({"指标": "低开弱反弹", "当前": f"低{open_gap*100:.2f}%后反弹{(price-today_open)/today_open*100:.2f}%<1%", "解读": "低开弱反弹，力度不足，建议避险卖出", "加分": 15})

        # V1.20: 开盘下杀后反弹无力/假突破（观察→确认→锁定）
        # 核心：开盘后迅速下杀，之后反弹但无力突破前收盘价/无法站稳，反弹高点是最佳止损点
        # 不是逐分钟加分，而是累积确认
        is_deep_loss_stop_loss = False
        if minutes_since_open <= 30:
            morning_low_so_far = float(df.iloc[:current_idx + 1]["low"].min()) if current_idx >= 0 else today_open
            morning_high_so_far = float(df.iloc[:current_idx + 1]["high"].max()) if current_idx >= 0 else today_open
            had_breakdown = morning_low_so_far < pre_close * 0.995
            rebound_from_low = (morning_high_so_far - morning_low_so_far) / morning_low_so_far if morning_low_so_far > 0 else 0
            pullback_from_high = (morning_high_so_far - price) / morning_high_so_far if morning_high_so_far > 0 else 0
            
            # 情况A：反弹从未突破前收盘价
            weak_reb_a_met = (had_breakdown and rebound_from_low > 0.002
                              and morning_high_so_far < pre_close * 0.995 and price < pre_close)
            # 情况B：反弹突破前收盘价但迅速回落
            weak_reb_b_met = (had_breakdown and rebound_from_low > 0.002
                              and morning_high_so_far >= pre_close * 0.995
                              and price < pre_close * 0.995 and pullback_from_high > 0.005)
            
            # 合并为一个"开盘反弹无力"因子观察
            weak_reb_met = weak_reb_a_met or weak_reb_b_met
            # 价格突破前收盘价且站稳则取消
            weak_reb_cancel = (price > pre_close and price > vwap) or strong_chop
            # ETF早盘：观察期加倍
            weak_reb_obs = 2 if not is_etf else (4 if t_val < 1000 else 2)
            weak_reb_confirmed, weak_reb_diag = self._check_scenario_factor(
                code, "WEAK_REBOUND", weak_reb_met, observation_minutes=weak_reb_obs,
                lock_minutes=30, etf_observation_multiplier=1.0,  # 已手动处理ETF
                cancel_condition=weak_reb_cancel
            )
            if weak_reb_confirmed:
                factor_name = "开盘反弹无力" if weak_reb_a_met else "开盘假突破"
                factor_desc = ("开盘下杀后反弹无力，无法突破前收盘价，反弹高点应全部止损"
                               if weak_reb_a_met else
                               "开盘下杀后反弹到前收盘价附近但无力站稳，假突破后应全部止损")
                if is_deep_loss:
                    # V1.26fix: 深套时只加止损分，不加"高抛"分
                    sell_score += 15
                    sell_details.append({"指标": factor_name + "(深套止损)", "当前": f"低{morning_low_so_far:.2f}→高{morning_high_so_far:.2f}→现{price:.2f}", "解读": f"深套{profit_pct*100:.1f}%：{factor_desc}，仅触发止损而非做T高抛", "加分": 15})
                else:
                    sell_score += 25
                    sell_details.append({"指标": factor_name, "当前": f"低{morning_low_so_far:.2f}→高{morning_high_so_far:.2f}→现{price:.2f}", "解读": factor_desc, "加分": 25})
                
                # V1.20: 深套止损（1分钟确认，但锁定后不再重复）
                if is_deep_loss:
                    deep_confirmed, deep_diag = self._check_scenario_factor(
                        code, "DEEP_LOSS_STOP", True, observation_minutes=1,
                        lock_minutes=60, etf_observation_multiplier=1.0,
                        cancel_condition=(profit_pct > -0.03)  # 亏损缩小到3%以内解锁
                    )
                    if deep_confirmed:
                        sell_score += 10  # V1.26fix: 从+20降至+10，避免深套股票误触发SELL_HIGH
                        sell_details.append({"指标": "深套止损", "当前": f"亏损{profit_pct*100:.1f}%", "解读": "深套股票开盘反弹无力，必须优先止损，额外+10", "加分": 10})
                        is_deep_loss_stop_loss = True

        # 5. MA_PRESSURE_SELL: 低开冲高均线压制（V1.20: 需观察2分钟，强势震荡抑制）
        ma_pressure_met = (open_gap < -0.01 and price > vwap * 0.995 and price < vwap * 1.005 and amplitude_gate)
        ma_pressure_cancel = strong_chop or (price > vwap * 1.005)  # 突破均线则取消
        ma_pressure_confirmed, ma_pressure_diag = self._check_scenario_factor(
            code, "MA_PRESSURE", ma_pressure_met, observation_minutes=2,
            lock_minutes=15, etf_observation_multiplier=etf_obs_mult,
            cancel_condition=ma_pressure_cancel
        )
        if ma_pressure_confirmed:
            if is_deep_loss:
                sell_score -= 15
                sell_details.append({"指标": "均线压制(深套抑制)", "当前": f"低{open_gap*100:.2f}%→均{vwap:.2f}未突破", "解读": f"深套{profit_pct*100:.1f}%：均线压制非做T高抛机会，抑制防止割肉", "加分": -15})
            else:
                sell_score += 12
                sell_details.append({"指标": "均线压制", "当前": f"低{open_gap*100:.2f}%→均{vwap:.2f}未突破", "解读": "低开冲高到均价线即遇阻，抛压明显，卖出", "加分": 12})

        # 6. LIMIT_UP_BLOCK: 涨停抑制
        if limit_up_triggered:
            sell_score -= 100
            sell_details.append({"指标": "涨停抑制", "当前": "当日曾涨停", "解读": "冲涨停炸板后谨慎，暂不建议卖出", "加分": -100})

        # 7. DOUBLE_TOP15: 日内双顶保护（V1.21: 增强——30min/1.5%/75%/RSI确认）
        if not strong_support and amplitude_gate:  # 强承接时抑制双顶卖出
            first_major_peak = 0
            peak_idx = -1
            dtp = p.get("double_top_pullback_threshold", 0.015)
            pullback_threshold = dtp
            min_gap_minutes = p.get("double_top_min_gap_minutes", 30)
            lookback_window = 60
            
            for j in range(max(1, current_idx - lookback_window), current_idx):
                h = float(df.iloc[j]["high"])
                if h <= first_major_peak:
                    continue
                low_after = float(df.iloc[j:current_idx + 1]["low"].min())
                if (h - low_after) / h >= pullback_threshold and (current_idx - j) >= min_gap_minutes:
                    first_major_peak = h
                    peak_idx = j
            
            vol_shrink_ok = True
            vol_shrink_threshold = p.get("double_top_vol_shrink_threshold", 0.75)
            if peak_idx > 0 and first_major_peak > 0:
                start_vol_idx = max(0, peak_idx - lookback_window)
                hist_vol = df.iloc[start_vol_idx:peak_idx]["volume"].astype(float)
                hist_vol_mean = hist_vol.mean() if len(hist_vol) > 0 else 0
                current_vol = float(df.iloc[current_idx]["volume"]) if "volume" in df.columns else 0
                if hist_vol_mean > 0 and current_vol > hist_vol_mean * vol_shrink_threshold:
                    vol_shrink_ok = False
            
            # V1.21: 增加RSI超买确认（前高附近RSI>=75）
            peak_rsi_ok = False
            double_top_rsi_threshold = p.get("double_top_rsi_threshold", 75)
            if peak_idx > 0 and "rsi" in df.columns:
                peak_rsi = float(df.iloc[peak_idx]["rsi"]) if not pd.isna(df.iloc[peak_idx]["rsi"]) else 0
                current_rsi = float(df.iloc[current_idx]["rsi"]) if not pd.isna(df.iloc[current_idx]["rsi"]) else 0
                if peak_rsi >= double_top_rsi_threshold or current_rsi >= double_top_rsi_threshold:
                    peak_rsi_ok = True
            else:
                peak_rsi_ok = True  # 若无RSI数据，跳过此项
            
            # V1.21: 收紧二次冲顶区间（0.99→0.995）
            double_top_price_proximity = p.get("double_top_price_proximity", 0.995)
            if first_major_peak > 0 and price >= first_major_peak * double_top_price_proximity and price < first_major_peak and vol_shrink_ok and peak_rsi_ok:
                sell_score += 25
                detail_type = "个股" if not is_etf else "ETF"
                sell_details.append({"指标": "双顶保护", "当前": f"前高{first_major_peak:.2f}→现{price:.2f}({detail_type})/RSI{current_rsi:.0f}", "解读": f"日内第一个高点后回落≥{pullback_threshold*100:.1f}%，二次冲顶未创新高，成交量萎缩，RSI超买，保护利润", "加分": 25})

        # V1.23fix: 二次冲高未突破前高检测（力量衰竭型卖点）
        # 案例：华工科技 2026-07-10 — 09:41前高168.98，09:54冲高168.62未突破前高
        # 区分：华工科技（冲高后明显回落再反弹未突破）vs 金风科技（高位强势整理）
        # 条件：当前价格接近前高但低于前高（差距<0.5%），且此前曾从高点明显回落（≥1%），且动量下降
        had_double_top = False
        if not is_etf and day_high_so_far > 0 and price > 0 and current_idx >= 5:
            peak_gap_pct = (day_high_so_far - price) / day_high_so_far
            # 距离前高很近（<0.5%）但低于前高，说明二次冲高未突破
            if 0 < peak_gap_pct < 0.005:
                # 确认此前曾从高点明显回落（至少1%）：排除一直在高点附近整理的股票
                peak_idx = int(df.iloc[:current_idx + 1]["high"].to_numpy().argmax()) if len(df) > 0 else current_idx
                low_after_peak = float(df.iloc[peak_idx:current_idx + 1]["low"].min()) if peak_idx < current_idx else price
                had_significant_pullback = low_after_peak <= day_high_so_far * 0.995  # V1.24fix: 放宽阈值0.99→0.995，捕获华工科技0.97%回落案例
                # 确认动量在下降：最近3分钟涨幅很小或下降（V1.23fix-R2: 放宽阈值0.001→0.003）
                momentum_weakening = rate3 < 0.003 or (current_idx >= 2 and price <= float(df.iloc[current_idx - 2]["close"]))
                if had_significant_pullback and momentum_weakening:
                    had_double_top = True
                    sell_score += 65  # V1.23fix-R2: 提高到65，确保华工科技型卖点能触发通知
                    sell_details.append({"指标": "二次冲高未突破", "当前": f"前高{day_high_so_far:.2f}→回落低{low_after_peak:.2f}→现{price:.2f}(差{peak_gap_pct*100:.2f}%)", "解读": "冲高后明显回落再反弹，二次冲高未能突破前高，力量衰竭，建议卖出", "加分": 65})
                    # V1.24: 二次冲高未突破 → 部分卖出（减仓）
                    diag.setdefault("pressure_support", {})
                    diag["pressure_support"]["sell_qty_pct"] = 50
                    diag["pressure_support"]["v1_23_triggered"] = True
                    # 早盘二次冲高额外加分，确保触发通知
                    if 930 <= t_val < 1000:
                        sell_score += 15
                        sell_details.append({"指标": "早盘冲高衰竭", "当前": f"t_val={t_val}", "解读": "早盘二次冲高未突破前高，力量衰竭卖点，额外加分", "加分": 15})

        sell_score = max(0, sell_score)

        # V1.23fix: 强势上涨形态抑制卖出（前置抑制）
        # 案例：金风科技 2026-07-10 — 从低点逐步反弹，均线多头排列+低点/高点抬高
        # 仅当不是二次冲高场景时触发（避免抑制华工科技型卖点）
        is_strong_uptrend = False
        if not is_etf and not had_double_top and len(df) >= 20 and price > 0:
            ma5_val = df["close"].tail(5).mean()
            ma10_val = df["close"].tail(10).mean()
            ma20_val = df["close"].tail(20).mean()
            # V1.23fix: 宽松版均线多头 — 允许MA5=MA10（如金风科技10:24）
            ma_aligned = ma5_val >= ma10_val and ma10_val >= ma20_val * 0.995
            # 从当日低点反弹幅度
            day_low_so_far = float(df["low"].iloc[:current_idx + 1].min()) if len(df) > 0 else price
            rebound_from_low = (price - day_low_so_far) / day_low_so_far if day_low_so_far > 0 else 0
            # 强势条件：均线多头排列 + 从低点反弹>3% + 在VWAP上方
            if ma_aligned and rebound_from_low > 0.03 and price > vwap * 1.005:
                is_strong_uptrend = True
                sell_score = max(0, sell_score - 80)  # V1.23fix: 大幅抑制卖出，-40→-80
                sell_threshold += 15  # V1.23fix: 提高卖出阈值，+12→+15
                sell_details.append({"指标": "强势上涨抑制卖出(前置)", "当前": f"MA5>{ma5_val:.2f}>MA10>{ma10_val:.2f}>MA20:{ma20_val:.2f}/反弹{rebound_from_low*100:.1f}%", "解读": "均线多头排列+从低点大幅反弹，强势上升通道中不应卖出，前置抑制", "加分": -80})
        # V1.23fix-R2: 删除宽松版二次冲高检测（与严格版重复，导致金风科技等强势股票误报）
        # 严格版二次冲高检测已在上文（1357-1379行）处理，要求had_significant_pullback

        sell_score = max(0, sell_score)

        # V1.23fix: 强势上涨形态抑制卖出
        # 案例：金风科技 2026-07-10 — 均线多头排列+不断创新高+涨停预期
        # V1.23fix-R2: 放宽条件 — 使用从低点反弹代替today_ret，允许MA5≈MA10
        if not is_etf and len(df) >= 20 and price > 0:
            ma5_val = df["close"].tail(5).mean()
            ma10_val = df["close"].tail(10).mean()
            ma20_val = df["close"].tail(20).mean()
            # 宽松版均线多头：MA5 >= MA10*0.995 且 MA10 >= MA20*0.995
            ma_aligned = ma5_val >= ma10_val * 0.995 and ma10_val >= ma20_val * 0.995
            # 最近10分钟高点持续抬高（允许0.5%回调）
            recent_10_highs = df["high"].tail(10).values
            higher_highs = all(recent_10_highs[i] >= recent_10_highs[i-1] * 0.995 for i in range(1, len(recent_10_highs)))
            # 从当日低点反弹幅度（代替today_ret，更适合判断盘中强势）
            day_low_so_far = float(df["low"].iloc[:current_idx + 1].min()) if len(df) > 0 else price
            rebound_from_low = (price - day_low_so_far) / day_low_so_far if day_low_so_far > 0 else 0
            # 强势条件：均线多头排列 + 创新高 + 在VWAP上方 + 从低点大幅反弹
            if ma_aligned and higher_highs and price > vwap * 1.005 and rebound_from_low > 0.03:
                sell_score = max(0, sell_score - 60)  # V1.23fix-R2: 抑制-40→-60
                sell_threshold += 15  # V1.23fix-R2: 阈值+12→+15
                sell_details.append({"指标": "强势上涨抑制卖出", "当前": f"MA5>{ma5_val:.2f}≈MA10>{ma10_val:.2f}≈MA20:{ma20_val:.2f}/低点反弹{rebound_from_low*100:.1f}%", "解读": "均线多头排列+不断创新高+从低点大幅反弹，强势上涨中不应卖出，抑制卖出信号", "加分": -60})
        # 案例：五洲新春 开盘后上方依次出现150日/30日/60日压力，多次冲击失败回落
        # 案例：摩恩电气 冲过MA10即回落
        # 案例：赛微电子 冲高到5日线附近出现抛压
        # 核心：从"单一切换"改为"累积观察"——统计压力区内的均线数量+冲击失败次数
        ma_resistance = None
        ma_resistance_boost = 0

        # 收集所有有效日线均线（包含中长周期）
        ma_candidates = [
            ("MA5", daily_ma5),
            ("MA10", daily_ma10),
            ("MA20", daily_ma20),
            ("MA30", daily_ma30),
            ("MA60", daily_ma60),
            ("MA120", daily_ma120),
            ("MA150", daily_ma150),
            ("MA180", daily_ma180),
            ("MA250", daily_ma250),
            ("MA365", daily_ma365),
        ]

        # 收集所有构成压力的均线（不break，全部遍历）
        pressure_mas = []
        for ma_name, ma_val in ma_candidates:
            if ma_val > 0 and price > ma_val * 0.95 and price < ma_val * 1.05:
                ma_gap = (price - ma_val) / ma_val
                # 压力判定：从下方接近均线，或刚突破但动能转负，或长上影线触及
                is_pressure = False
                ptype = ""
                if ma_gap < 0 and abs(ma_gap) < 0.025 and (today_ret > 0.005 or mom5 > 0):
                    is_pressure = True
                    ptype = "approaching"
                elif ma_gap >= 0 and ma_gap < 0.02 and mom5 < 0:
                    is_pressure = True
                    ptype = "breach_stall"
                elif abs(ma_gap) < 0.015 and upper_shadow > 0.3:
                    is_pressure = True
                    ptype = "upper_shadow"

                if is_pressure:
                    pressure_mas.append({
                        "name": ma_name,
                        "level": ma_val,
                        "gap_pct": ma_gap,
                        "type": ptype,
                    })

        pressure_count = len(pressure_mas)
        if pressure_count >= 1:
            # === 压力密集度判断 ===
            levels = [p["level"] for p in pressure_mas]
            max_level = max(levels) if levels else 0
            min_level = min(levels) if levels else 0
            cluster_span = (max_level - min_level) / max_level if max_level > 0 else 999
            is_cluster = cluster_span < 0.05  # 5%以内视为密集区

            # === 冲击失败检测（从分钟线推断） ===
            shock_fail_boost = 0
            fail_note = ""
            if len(df) >= 10 and levels:
                pressure_upper = max_level * 1.01
                pressure_lower = min_level * 0.99
                recent_df = df.tail(15) if len(df) >= 15 else df
                # 统计价格高点触及/进入压力区的次数
                touch_count = sum(1 for h in recent_df["high"] if pressure_lower <= h <= pressure_upper)
                # 当前价格是否已回落到压力区下方
                current_below = price < pressure_lower
                if touch_count >= 3 and current_below:
                    shock_fail_boost = 12
                    fail_note = f"最近15分钟冲击{touch_count}次均失败"
                elif touch_count >= 2 and current_below:
                    shock_fail_boost = 8
                    fail_note = f"最近15分钟冲击{touch_count}次失败"
                elif touch_count >= 1 and current_below:
                    shock_fail_boost = 5
                    fail_note = "最近冲击1次未站稳"

            # === V1.15fix: ETF门控 — 仅强压力触发 ===
            # 案例：科创半导体ETF频繁报高抛，但ETF走势平滑，单条均线压力多为虚假信号
            if is_etf:
                # ETF: 1条均线压力 → 忽略（虚假信号多）
                # ETF: 2+条均线 → 必须密集区或冲击失败才触发
                # ETF: 3+条均线 → 正常触发（抛压极重）
                if pressure_count == 1:
                    pressure_count = 0
                    pressure_mas = []
                elif pressure_count == 2 and not is_cluster and shock_fail_boost < 8:
                    pressure_count = 0
                    pressure_mas = []
                # 3+条均线或密集区或冲击失败 → 通过门控

            # === 动态加分计算（根据压力数量+密集度+冲击失败） ===
            if pressure_count >= 1:
                if pressure_count == 1:
                    base_boost = 12
                elif pressure_count == 2:
                    base_boost = 18
                elif pressure_count >= 3:
                    base_boost = 22

                cluster_boost = 8 if is_cluster else 0
                ma_resistance_boost = base_boost + cluster_boost + shock_fail_boost
                sell_score += ma_resistance_boost

                # 构建详细描述
                pressure_names = "/".join([p["name"] for p in pressure_mas])
                main_ma = pressure_mas[0]  # 最接近价格的
                detail_text = f"检测到{pressure_count}条均线压力"
                if is_cluster:
                    detail_text += "（密集区）"
                if fail_note:
                    detail_text += f"，{fail_note}"

                sell_details.append({
                    "指标": f"多均线压力({pressure_names})",
                    "当前": f"{price:.2f} vs {main_ma['level']:.2f}({main_ma['gap_pct']*100:+.2f}%)",
                    "解读": detail_text + "，多次冲击未果，抛压累积，高概率回落",
                    "加分": ma_resistance_boost,
                })

                ma_resistance = {
                    "pressure_count": pressure_count,
                    "pressure_mas": pressure_mas,
                    "cluster_span": cluster_span,
                    "is_cluster": is_cluster,
                    "fail_note": fail_note,
                    "boost": ma_resistance_boost,
                }

        indicators["ma_resistance"] = ma_resistance

        sig = None
        is_dead_water = (day_amplitude < p["min_amplitude"] and t_val > 1000)

        # === V1.14: 华工科技 0701 四大教训改进（核心风控增强）===
        cost = float(holding.get("cost", 0) or 0)
        profit_pct = (price - cost) / cost if cost > 0 else 0

        # 教训1: 成本逼近保护 — 利润微薄时冲高优先保住利润
        is_tight_profit = cost > 0 and 0 < profit_pct < p.get("profit_guard_tight_profit_max", 0.03)
        is_tight_gap = cost > 0 and abs(price - cost) / cost < p.get("profit_guard_tight_gap_max", 0.015)
        if (is_tight_profit or is_tight_gap) and price > vwap and range_pos > 0.7:
            boost = p.get("profit_guard_sell_boost", 15)
            sell_score += boost
            sell_details.append({
                "指标": "成本逼近保护",
                "当前": f"利润{profit_pct*100:.2f}%/价差{abs(price-cost)/cost*100:.1f}%",
                "解读": "利润逼近成本，冲高优先保住利润，防止坐电梯",
                "加分": boost,
            })
            buy_threshold += p.get("profit_guard_buy_penalty", 10)

        # 教训2: 盘口超大单背离 — 价格冲高但动能/量能不配合，最早分时预警
        is_surging = today_ret > 0.02 and price > prev_high * 0.995
        has_divergence = is_surging and (mom5 < 0.005 or upper_shadow > 0.35 or (macd_hist < prev_macd_hist and macd_hist > 0))
        if has_divergence:
            boost = p.get("surge_shadow_divergence_boost", 12)
            sell_score += boost
            sell_details.append({
                "指标": "冲高背离",
                "当前": f"涨{today_ret*100:.2f}%/5分mom{mom5*100:.2f}%/上影{upper_shadow:.2f}",
                "解读": "价格冲高但动能/量能不配合，疑似主力派发，分时最早预警",
                "加分": boost,
            })

        # 教训3: 压力区检测 — 突破布林上轨+远离MA5，前期高点容易stalled
        daily_ma5 = daily_ctx.get("daily_ma5", 0.0)
        bb_breakout = bb_pct > 0.95
        ma5_far = daily_ma5 > 0 and price > daily_ma5 * 1.05
        if bb_breakout and ma5_far:
            boost = p.get("bb_band_breakout_penalty", 8) + p.get("ma5_deviation_sell_boost", 10)
            sell_score += boost
            sell_details.append({
                "指标": "压力区突破",
                "当前": f"布林{bb_pct:.2f}/距MA5+{(price/daily_ma5-1)*100:.1f}%",
                "解读": "价格突破布林上轨且远离MA5，进入压力区，前期高点容易 stalled",
                "加分": boost,
            })

        # 教训4: 大跌后反抽识别 — 前日大跌今日反拉是反抽不是重启
        prev_day_ret = daily_ctx.get("daily_prev_day_ret", 0.0)
        # 【V1.15fix-4】当 daily_context 不可用时，使用 holdings pre_close 推断前日涨跌
        if prev_day_ret == 0.0 and daily_status in {"insufficient", "unavailable", "error"} and pre_close > 0 and today_open > 0:
            inferred_prev_day_ret = (today_open - pre_close) / pre_close
            if inferred_prev_day_ret < -0.05:
                prev_day_ret = inferred_prev_day_ret
        is_big_drop_bounce = prev_day_ret < p.get("big_drop_bounce_threshold", -0.05) and today_ret > 0.02
        if is_big_drop_bounce:
            boost = p.get("big_drop_bounce_sell_boost", 10)
            sell_score += boost
            sell_details.append({
                "指标": "大跌反抽",
                "当前": f"昨日{prev_day_ret*100:.1f}%/今日+{today_ret*100:.1f}%",
                "解读": "前日大跌后今日反抽，非突破而是派发窗口，低仓位弱板=洗盘，高仓位天量弱板=出货",
                "加分": boost,
            })
            buy_threshold += p.get("big_drop_bounce_buy_penalty", 5)

        # 【V1.15fix-5】前日大跌后延续下跌：抑制买入 + 优先倒T（减少损失）
        if prev_day_ret < -0.05:
            # 前日大跌，次日整体以防守为主
            buy_score -= 15
            buy_details.append({
                "指标": "前日大跌抑制",
                "当前": f"昨日跌{prev_day_ret*100:.1f}%",
                "解读": "前日大跌后，次日做T以防守为主，抑制低吸，优先寻找高抛机会做倒T",
                "加分": -15,
            })
            buy_threshold += 10
            # 如果当日继续下跌（跌>1%），进一步抑制买入并鼓励卖出
            if today_ret < -0.01:
                buy_score -= 15
                buy_details.append({
                    "指标": "延续下跌",
                    "当前": f"今日跌{today_ret*100:.1f}%",
                    "解读": "前日大跌后当日继续下跌，下降趋势确认，强烈建议观望或优先高抛减少损失",
                    "加分": -15,
                })
                # 【V1.15fix】下降趋势买入阈值提升：在ETF阈值保护后重新生效
                # sell_score += 10 保留，鼓励卖出
                sell_score += 10
                sell_details.append({
                    "指标": "下跌趋势高抛",
                    "当前": f"今日跌{today_ret*100:.1f}%",
                    "解读": "下降趋势中，优先寻找高抛机会做倒T，减少持仓损失",
                    "加分": 10,
                })
        # V1.24: 双良节能倒T信号增强 — 下跌趋势中早盘高抛额外加分
        if p.get("downtrend_sell_boost", 0) > 0 and today_ret < p.get("downtrend_sell_threshold", 0.02):
            boost = p.get("downtrend_sell_boost", 10)
            sell_score += boost
            sell_details.append({
                "指标": "倒T信号增强",
                "当前": f"今日跌{today_ret*100:.1f}%",
                "解读": "下跌趋势中，早盘任何反弹都是高抛做倒T机会，增强卖出信号",
                "加分": boost,
            })

        # 【V1.16】多周期趋势判断：基于日线/周线/月线的共振评估
        # 当 multi_tf_dict 可用时，增加多周期趋势因子
        if multi_tf_dict and multi_tf_dict.get("trend_direction"):
            tf_dir = multi_tf_dict["trend_direction"]
            tf_risk = multi_tf_dict.get("risk_level", "low")
            tf_alignment = multi_tf_dict.get("trend_alignment", 0)
            
            # 强空头：严禁低吸，优先做倒T
            if tf_dir == "strong_down":
                buy_score -= 20
                buy_details.append({
                    "指标": "多周期强空头",
                    "当前": f"共振得分{tf_alignment}/5",
                    "解读": "日线/周线/月线均走弱，严禁低吸，优先做倒T减少损失",
                    "加分": -20,
                })
                buy_threshold += 15
                # V1.21fix: 强势震荡中，不鼓励"强空头优先高抛"，避免过早卖出
                if not strong_chop:
                    sell_score += 15
                    sell_details.append({
                        "指标": "强空头优先高抛",
                        "当前": f"风险等级:{tf_risk}",
                        "解读": "多周期共振下行，任何反弹都是高抛机会",
                        "加分": 15,
                    })
                else:
                    sell_details.append({
                        "指标": "强空头优先高抛(抑制)",
                        "当前": f"风险等级:{tf_risk}",
                        "解读": "强势震荡中，多周期空头信号被抑制",
                        "加分": 0,
                    })
            # 空头：抑制买入，鼓励倒T
            elif tf_dir == "down":
                buy_score -= 10
                buy_details.append({
                    "指标": "多周期空头",
                    "当前": f"共振得分{tf_alignment}/5",
                    "解读": "趋势偏弱，低吸需谨慎，仅尾盘超跌接回",
                    "加分": -10,
                })
                buy_threshold += 8
                if tf_risk in {"high", "critical"}:
                    buy_threshold += 10
                    sell_score += 10
                    sell_details.append({
                        "指标": "高风险抛售",
                        "当前": f"风险等级:{tf_risk}",
                        "解读": "多周期风险累积，优先做倒T",
                        "加分": 10,
                    })
            # 强趋势中的洗盘：放宽买入
            elif tf_dir == "strong_up" and prev_day_ret < -0.05:
                buy_score += 10
                buy_details.append({
                    "指标": "强趋势中的洗盘",
                    "当前": f"昨日跌{prev_day_ret*100:.1f}%",
                    "解读": "多周期强势中的单日大跌，可能是洗盘，可积极低吸",
                    "加分": 10,
                })
                buy_threshold -= 5
            
            # 周线高位：警惕回调
            weekly_pos = multi_tf_dict.get("weekly_position", "")
            weekly_prev = multi_tf_dict.get("weekly_prev_ret", 0.0)
            if weekly_pos == "above_ma5" and weekly_prev > 0.05:
                sell_score += 8
                sell_details.append({
                    "指标": "周线高位",
                    "当前": f"周涨{weekly_prev*100:.1f}%",
                    "解读": "周线已大幅上涨，警惕周线级别回调",
                    "加分": 8,
                })
            
            # 月线破位：长期趋势破坏
            monthly_pos = multi_tf_dict.get("monthly_position", "")
            if monthly_pos == "below_ma3":
                buy_score -= 15
                buy_details.append({
                    "指标": "月线破位",
                    "当前": f"价格低于月线MA3:{multi_tf_dict.get('monthly_ma3', 0):.2f}",
                    "解读": "长期趋势已破坏，做T应以防守为主，减少仓位",
                    "加分": -15,
                })
                buy_threshold += 12

        # V1.12: ETF专属加分与阈值保护
        if is_etf:
            # ETF额外加分：降低触发门槛
            buy_score += p.get("etf_buy_score_boost", 5)
            sell_score += p.get("etf_sell_score_boost", 3)
            if buy_score > 0:
                buy_details.append({"指标": "ETF T+0加成", "当前": f"+{p.get('etf_buy_score_boost', 5)}", "解读": "ETF高频交易专属加分", "加分": p.get("etf_buy_score_boost", 5)})
            if sell_score > 0:
                sell_details.append({"指标": "ETF T+0加成", "当前": f"+{p.get('etf_sell_score_boost', 3)}", "解读": "ETF高频交易专属加分", "加分": p.get("etf_sell_score_boost", 3)})
            # ETF阈值硬性上限保护
            buy_threshold = min(buy_threshold, p.get("etf_threshold_cap", 38))
            sell_threshold = min(sell_threshold, p.get("etf_threshold_cap", 38))

        # 【V1.15fix-5】下降趋势买入阈值提升：在ETF阈值保护后重新生效
        if prev_day_ret < -0.05:
            buy_threshold += 15
            if today_ret < -0.01:
                buy_threshold += 10

        # V1.11: 在阈值计算后记录早盘冲高/下午回落日志
        if _log_enhancer:
            if had_morning_surge:
                _log_enhancer.log_morning_surge(
                    code=code, name=name, stage="detected" if sell_score < sell_threshold else "triggered",
                    price=price, vwap=vwap, today_ret=today_ret,
                    sell_score=sell_score, sell_threshold=sell_threshold,
                    factors=["早盘冲高"], is_triggered=sell_score >= sell_threshold
                )
            if had_afternoon_pullback:
                _log_enhancer.log_afternoon_pullback(
                    code=code, name=name, stage="detected" if buy_score < buy_threshold else "triggered",
                    price=price, vwap=vwap, rsi=rsi,
                    buy_score=buy_score, buy_threshold=buy_threshold, factors=["下午回落"]
                )
        diag["buy_block_reasons"] = []
        diag["sell_block_reasons"] = []
        diag["priority_path"] = "hold"
        diag["preempted_by_sell_fast_path"] = False
        diag["buy_candidate"] = False
        diag["sell_candidate"] = False

        buy_factor_map = {d["指标"]: d for d in buy_details}
        sell_factor_map = {d["指标"]: d for d in sell_details}
        hold_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
        buy_today_count = self.buy_count_per_stock.get(code, 0)
        sell_today_count = self.sell_count_per_stock.get(code, 0)
        can_buy_more = buy_today_count < p["max_buy_times_per_stock"]
        can_sell_today = sell_today_count < p["max_sell_times_per_stock"]
        can_sell = hold_qty > 0 and can_sell_today
        buy_limit_reason = ""
        if buy_today_count >= p["max_buy_times_per_stock"]:
            buy_limit_reason = f"已达当日买入上限{p['max_buy_times_per_stock']}次"
        # V1.14: 日内弱势盘中禁买 — 价格低于均价，14:30前禁止低吸
        if price < vwap and t_val < 1430:
            buy_limit_reason = "价格低于均价，14:30前禁止买入"
        sell_limit_reason = "" if can_sell_today else f"已达当日卖出上限{p['max_sell_times_per_stock']}次"
        net_qty = self._virtual_net_qty(code, holding)
        last_state = self.last_signal_state.get(code, {})
        base_memory = _strategy_memory_for_code(code)
        index_regime_status = daily_ctx.get("index_regime_status", "missing")
        index_circuit_state = daily_ctx.get("index_circuit_state", "normal")
        index_gate_advice = daily_ctx.get("index_gate_advice", "normal_t")
        index_pos_factor = float(daily_ctx.get("index_pos_factor", 1.0) or 1.0)
        index_temp_bucket = daily_ctx.get("index_temp_bucket", "neutral")
        index_score_delta = float(daily_ctx.get("index_score_delta", 0.0) or 0.0)
        index_regime = daily_ctx.get("index_regime", "range")

        if benchmark_gate == "weak":
            buy_threshold += 5
            if not is_strong_pullback:
                buy_threshold += 4
            sell_threshold += 1
        elif benchmark_gate == "strong":
            buy_threshold -= 1
            if not is_strong_pullback:
                sell_threshold += 3

        if index_regime_status == "ok":
            if index_circuit_state == "clear":
                buy_threshold += int(PARAMS.get("index_clear_sell_boost", 100))
                sell_threshold = max(35, sell_threshold - int(PARAMS.get("index_defensive_sell_relief", 3)))
            elif index_circuit_state == "reduce":
                buy_threshold += int(PARAMS.get("index_freeze_buy_penalty", 100))
                sell_threshold = max(35, sell_threshold - int(PARAMS.get("index_reduce_sell_boost", 10)))
            elif index_circuit_state == "defensive":
                buy_threshold += int(PARAMS.get("index_defensive_buy_penalty", 8))
                sell_threshold = max(35, sell_threshold - int(PARAMS.get("index_defensive_sell_relief", 3)))
            elif index_circuit_state == "stand_aside":
                buy_threshold += int(PARAMS.get("index_freeze_buy_penalty", 100))
            if index_temp_bucket == "cold":
                buy_threshold += int(PARAMS.get("index_defensive_buy_penalty", 8))
            if index_temp_bucket in {"freeze", "clear"}:
                buy_threshold += int(PARAMS.get("index_freeze_buy_penalty", 100))
            if index_gate_advice == "trend_up_hold" and index_pos_factor > 1.0:
                buy_threshold = max(35, buy_threshold - 2)
            if index_score_delta <= float(PARAMS.get("index_deterioration_delta", -10.0)) and index_regime == "uni_down":
                buy_threshold += 5

        daily_status = daily_ctx.get("daily_status", "unknown")
        daily_gate = daily_ctx.get("daily_gate", "neutral")
        daily_trend_bg = daily_ctx.get("daily_trend_bg", "unknown")
        daily_support_gap = float(daily_ctx.get("daily_support_gap", 0.0) or 0.0)
        daily_breakdown_risk = bool(daily_ctx.get("daily_breakdown_risk", False))
        daily_hard_breakdown = bool(daily_ctx.get("daily_hard_breakdown", False))
        daily_overheated = bool(daily_ctx.get("daily_overheated", False))
        daily_pullback_support = bool(daily_ctx.get("daily_pullback_support", False))
        daily_near_support = bool(daily_ctx.get("daily_near_support", False))
        index_regime_status = daily_ctx.get("index_regime_status", "missing")
        index_circuit_state = daily_ctx.get("index_circuit_state", "normal")
        index_gate_advice = daily_ctx.get("index_gate_advice", "normal_t")
        index_pos_factor = float(daily_ctx.get("index_pos_factor", 1.0) or 1.0)
        index_temp_bucket = daily_ctx.get("index_temp_bucket", "neutral")
        index_score_delta = float(daily_ctx.get("index_score_delta", 0.0) or 0.0)
        index_regime = daily_ctx.get("index_regime", "range")
        if daily_status == "ok":
            if daily_gate == "risk" or daily_hard_breakdown:
                buy_threshold += p["daily_risk_buy_threshold_penalty"]
                sell_threshold = max(35, sell_threshold - 1)
            elif daily_gate == "overheat":
                buy_threshold += p["daily_overheat_buy_threshold_penalty"]
                if not is_strong_pullback:
                    buy_threshold += 2
                sell_threshold += 2
            elif daily_gate == "supportive" and range_pos <= 0.45:
                buy_threshold = max(35, buy_threshold - p["daily_support_buy_threshold_relief"])
            elif daily_gate == "caution":
                buy_threshold += 2

            if daily_trend_bg in {"bull", "uptrend"}:
                buy_score += p["daily_trend_buy_boost"]
                buy_details.append({"指标": "日线趋势背景", "当前": daily_trend_bg, "阈值": "多头/上行", "加分": PARAMS["daily_trend_buy_boost"]})
            elif daily_trend_bg == "base":
                buy_score += p["daily_base_buy_boost"]
                buy_details.append({"指标": "日线底座", "当前": "均线粘合", "阈值": "底部整理", "加分": PARAMS["daily_base_buy_boost"]})
            elif daily_trend_bg in {"downtrend", "weak_breakdown"}:
                buy_score -= p["daily_downtrend_buy_penalty"]
                buy_details.append({"指标": "日线偏弱", "当前": daily_trend_bg, "阈值": "日线走弱", "加分": -PARAMS["daily_downtrend_buy_penalty"]})

            if daily_pullback_support or (daily_near_support and range_pos <= 0.55 and price <= vwap):
                buy_score += p["daily_support_buy_boost"]
                buy_details.append({"指标": "日线回踩支撑", "当前": f"{daily_ctx.get('daily_support_name', '')}@{daily_support_gap*100:.2f}%", "阈值": "MA20/30/60附近", "加分": PARAMS["daily_support_buy_boost"]})
            elif daily_near_support:
                buy_score += p["daily_base_buy_boost"]
                buy_details.append({"指标": "日线靠近支撑", "当前": f"{daily_ctx.get('daily_support_name', '')}@{daily_support_gap*100:.2f}%", "阈值": "支撑附近", "加分": PARAMS["daily_base_buy_boost"]})

            if daily_breakdown_risk:
                buy_score -= p["daily_breakdown_buy_penalty"]
                buy_details.append({"指标": "日线破位风险", "当前": daily_trend_bg, "阈值": "跌破关键均线", "加分": -PARAMS["daily_breakdown_buy_penalty"]})
                sell_score += p["daily_breakdown_sell_boost"]
                sell_details.append({"指标": "日线破位风险", "当前": daily_trend_bg, "阈值": "跌破关键均线", "加分": PARAMS["daily_breakdown_sell_boost"]})
            if daily_hard_breakdown:
                buy_score -= p["daily_breakdown_buy_penalty"]
                sell_score += p["daily_breakdown_sell_boost"] + 3
                sell_details.append({"指标": "日线硬破位", "当前": daily_trend_bg, "阈值": "MA60下方", "加分": PARAMS["daily_breakdown_sell_boost"] + 3})
            if daily_overheated:
                buy_score -= p["daily_overheat_buy_penalty"]
                buy_details.append({"指标": "日线过热", "当前": daily_trend_bg, "阈值": "远离MA10/20", "加分": -PARAMS["daily_overheat_buy_penalty"]})
                sell_score += p["daily_overheat_sell_boost"]
                sell_details.append({"指标": "日线过热", "当前": daily_trend_bg, "阈值": "远离MA10/20", "加分": PARAMS["daily_overheat_sell_boost"]})

        if market_state == "trend_down":
            buy_threshold += p["market_state_threshold_bias"] + 4
            sell_threshold = max(38, sell_threshold - 1)
        elif market_state == "trend_up":
            if not is_strong_pullback:
                buy_threshold += 3
            sell_threshold += 3

        buy_threshold, sell_threshold, buy_score, sell_score = _special_loss_threshold_adjustments(
            code,
            "BUY_LOW" if buy_score >= buy_threshold else ("SELL_HIGH" if sell_score >= sell_threshold else "HOLD"),
            buy_threshold,
            sell_threshold,
            buy_score,
            sell_score,
            price,
            vwap,
            is_strong_pullback,
        )

        cycle_count = self.cycle_count.get(code, 0)
        if cycle_count >= p["max_t_cycles_per_stock"]:
            buy_threshold += 100
            sell_threshold += 100
        elif cycle_count == 1:
            buy_threshold += 8
            sell_threshold += 8

        if buy_today_count >= p["max_buy_times_per_stock"]:
            buy_threshold += 100
        if sell_today_count >= p["max_sell_times_per_stock"]:
            sell_threshold += 100

        if sell_today_count == 0 and buy_today_count == 0:
            buy_threshold += 2
            sell_threshold += 2

        if index_regime_status == "ok" and index_circuit_state == "clear" and hold_qty > 0 and net_qty > 0:
            sell_score += 100
            sell_threshold = max(35, sell_threshold - 20)
            diag["index_circuit_forced_sell"] = True
        if index_regime_status == "ok" and index_circuit_state in {"reduce", "clear"} and hold_qty > 0 and net_qty > 0:
            sell_score += int(PARAMS.get("index_reduce_sell_boost", 10))
        if hold_qty <= 0:
            sell_score = -999
        if net_qty <= 0:
            sell_score = -999
        last_trade = self.last_trade_state.get(code, {})
        post_sell_block_until = self.post_sell_block_until.get(code)
        post_sell_block_active = bool(post_sell_block_until and _now() < post_sell_block_until)
        post_sell_block_remaining = (post_sell_block_until - _now()).total_seconds() if post_sell_block_active else 0.0
        post_sell_rebuild_allowed = False
        post_sell_rebuild_reason = ""
        if post_sell_block_active and hold_qty > 0:
            price_rebuild_ok = bool(vwap) and price <= vwap * (1 + p["post_sell_rebuild_price_gap"])
            score_rebuild_ok = (buy_score - sell_score) >= max(0, p["post_sell_rebuild_score_gap"] - p["post_sell_rebuild_relax_gap"] - 4)
            post_sell_elapsed = p["post_sell_rebuild_minutes"] * 60 - post_sell_block_remaining
            time_rebuild_ok = post_sell_elapsed >= p["post_sell_rebuild_min_seconds"]
            post_sell_rebuild_allowed = price_rebuild_ok and score_rebuild_ok and time_rebuild_ok
            parts = []
            if price_rebuild_ok:
                parts.append("price")
            if score_rebuild_ok:
                parts.append("score")
            if time_rebuild_ok:
                parts.append("time")
            post_sell_rebuild_reason = ",".join(parts) if parts else "blocked"
            if not post_sell_rebuild_allowed:
                buy_threshold += p["post_sell_rebuild_buy_threshold_penalty"]
                if benchmark_gate == "weak":
                    buy_threshold += int(round(3 * p["post_sell_rebuild_weak_gate_discount"]))
                sell_threshold += 20
        # V1.21: 高抛后低吸闭环——卖出后降低买入阈值，优先提示低吸
        awaiting_buyback = self.awaiting_buyback.get(code)
        ab_active = bool(awaiting_buyback) and _now() < awaiting_buyback["expires"] and hold_qty > 0
        if ab_active:
            sell_price = awaiting_buyback["sell_price"]
            sell_vwap = awaiting_buyback.get("sell_vwap", vwap)
            ab_price_gap = p.get("awaiting_buyback_price_gap", 0.003)
            ab_vwap_gap = p.get("awaiting_buyback_vwap_gap", 0.998)
            ab_rsi_strong = p.get("awaiting_buyback_rsi_strong", 45)
            ab_rsi_weak = p.get("awaiting_buyback_rsi_weak", 50)
            ab_score_boost = p.get("awaiting_buyback_score_boost", 10)
            ab_score_boost_weak = p.get("awaiting_buyback_score_boost_weak", 5)
            ab_threshold_relax = p.get("awaiting_buyback_threshold_relax", 5)
            ab_threshold_relax_weak = p.get("awaiting_buyback_threshold_relax_weak", 3)
            price_below_sell = price <= sell_price * (1 - ab_price_gap)
            price_below_vwap = bool(vwap) and price < vwap * ab_vwap_gap
            if price_below_sell and price_below_vwap and rsi < ab_rsi_strong:
                buy_score += ab_score_boost
                buy_details.append({"指标": "卖后低吸", "当前": f"卖出价{sell_price:.2f}→现{price:.2f}/VWAP{vwap:.2f}", "解读": "高抛后价格回落到VWAP下方，RSI超卖，鼓励低吸降成本", "加分": ab_score_boost})
                buy_threshold -= ab_threshold_relax
            elif price_below_sell and rsi < ab_rsi_weak:
                buy_score += ab_score_boost_weak
                buy_details.append({"指标": "卖后低吸(弱)", "当前": f"卖出价{sell_price:.2f}→现{price:.2f}", "解读": "高抛后价格回落，等待更低位置", "加分": ab_score_boost_weak})
                buy_threshold -= ab_threshold_relax_weak
            # V1.21fix: 如果已经回落到区间低位且低点抬高，进一步降低阈值
            if range_pos <= 0.30 and strong_chop:
                buy_threshold -= 8
                buy_score += 8
                buy_details.append({"指标": "卖后区间低位", "当前": f"range_pos={range_pos:.2f}/低点抬高", "解读": "高抛后回落到区间低位，主力运作明显，强力低吸", "加分": 8})
            # V1.21fix: 如果RSI严重超卖，进一步降低阈值
            if rsi <= 35:
                buy_threshold -= 5
                buy_score += 5
                buy_details.append({"指标": "卖后RSI超卖", "当前": f"RSI={rsi:.1f}", "解读": "高抛后RSI严重超卖，强力低吸", "加分": 5})
            # V1.21fix: 高抛后等待低吸时，放宽价格条件（即使价格低于VWAP也允许买入）
        if hold_qty > 0 and can_buy_more and last_state.get("action") in ["BUY_LOW", "ADD_POS"]:
            buy_threshold += 3
        # V1.15: 最优卖点判断（提前定义，避免冗余检查引用时未绑定）
        optimal_sell_conditions = sum([
            range_pos >= p.get("optimal_sell_range_pos", 0.95),
            rsi >= p.get("optimal_sell_rsi", 85),
            bb_pct >= p.get("optimal_sell_bb_pct", 0.90),
            today_ret >= p.get("optimal_sell_today_ret", 0.02),
        ])
        is_optimal_sell = optimal_sell_conditions >= 2

        # V1.21: 高点确认延迟——防止冲高中途抖动误卖
        peak_confirmed = True
        peak_decline_penalty = 0
        if strong_chop:
            # V1.21fix: 强势震荡中，抑制高点确认卖出
            peak_confirmed = False
            sell_score -= 30  # V1.21fix: 从-10加大到-30，更严格抑制
            sell_threshold += 15  # V1.21fix: 强势震荡中提高卖出阈值
            sell_details.append({"指标": "强势震荡抑高", "当前": f"range_pos={range_pos:.2f}", "解读": "强势震荡中，价格围绕均线波动，抑制高点确认卖出", "加分": -30})
        elif not is_optimal_sell:
            peak_lookback = 5
            peak_price = price
            peak_idx = current_idx
            for j in range(max(0, current_idx - peak_lookback), current_idx + 1):
                h = float(df.iloc[j]["high"])
                if h > peak_price:
                    peak_price = h
                    peak_idx = j
            peak_decline_pct = (peak_price - price) / peak_price if peak_price > 0 else 0
            peak_decline_minutes = current_idx - peak_idx
            peak_decline_pct_threshold = p.get("peak_decline_pct_threshold", 0.01)
            peak_decline_min_minutes = p.get("peak_decline_min_minutes", 3)
            # V1.21fix: 提高高点确认门槛——回落需≥1.5%或持续≥5分钟
            peak_decline_pct_threshold = max(peak_decline_pct_threshold, 0.015)
            peak_decline_min_minutes = max(peak_decline_min_minutes, 5)
            if peak_decline_pct < peak_decline_pct_threshold or peak_decline_minutes < peak_decline_min_minutes:
                peak_confirmed = False
                peak_decline_penalty = p.get("peak_decline_penalty", 5)
                sell_score -= peak_decline_penalty
                sell_threshold += peak_decline_penalty  # V1.21fix: 高点未确认时提高卖出阈值，避免过早卖出
                sell_details.append({"指标": "高点未确认", "当前": f"距最高{peak_price:.2f}回落{peak_decline_pct*100:.1f}%/{peak_decline_minutes}分钟", "解读": "冲高中途小幅回落，等待高点确认后再卖", "加分": -peak_decline_penalty})
        # ==================== V1.19: 卖出幅度过滤 ====================
        # 用户反馈：华丰科技9:43高抛幅度太小，不应触发
        min_sell_profit_space = p.get("min_sell_profit_space", 0.005)
        # V1.19: 早盘（10:00前）要求更高幅度
        if t_val < 1000:
            min_sell_profit_space = max(min_sell_profit_space, 0.008)
        if sell_profit_space < min_sell_profit_space and not is_optimal_sell and not is_deep_loss:
            sell_score -= 30  # V1.19: 从-25加大到-30
            sell_details.append({"指标": "幅度不足过滤", "当前": f"{sell_profit_space*100:.2f}%<{min_sell_profit_space*100:.2f}%", "解读": "冲高幅度太小，非有效高抛点", "加分": -30})
        elif is_deep_loss and sell_profit_space < min_sell_profit_space:
            sell_details.append({"指标": "深套止损豁免", "当前": f"亏损{profit_pct*100:.1f}%/{sell_profit_space*100:.2f}%", "解读": "深套股票反弹高点，优先止损，豁免幅度过滤", "加分": 0})
        
        # V1.15: 最优卖点绕过重复信号检查（避免在最高点被 redundant 封锁）
        if hold_qty > 0 and self._is_redundant_signal(code, "SELL_HIGH", price, sell_score):
            if not is_optimal_sell:
                sell_threshold += 120
                sell_score -= 8
                diag["sell_block_reasons"].append("redundant_signal")
            else:
                diag["optimal_sell_bypass_redundant"] = True
        if self._is_redundant_signal(code, "BUY_LOW", price, buy_score):
            buy_threshold += 100
        if self._is_redundant_signal(code, "ADD_POS", price, buy_score):
            buy_threshold += 100
        if last_state.get("action") in ["SELL_HIGH", "PANIC_SELL"] and hold_qty > 0:
            buy_threshold += 4
        if last_state.get("action") in ["BUY_LOW", "ADD_POS"] and hold_qty > 0:
            sell_threshold += 4
        if last_trade.get("action") in ["BUY_LOW", "ADD_POS"] and holding_minutes < 8:
            sell_threshold += 3
        if last_trade.get("action") in ["SELL_HIGH", "PANIC_SELL"] and holding_minutes < 8:
            buy_threshold += 8

        stand_down, stand_down_reason = self._should_stand_down(code, holding, df, buy_score, sell_score, market_state, can_sell, today_ret, minutes_since_open)
        if stand_down:
            buy_threshold += 100
            sell_threshold += 100
            diag["buy_block_reasons"].append(stand_down_reason)
            diag["sell_block_reasons"].append(stand_down_reason)
            sig = None

        if post_sell_block_active:
            dec = DAILY_DECISION_STATS.get(code)
            if dec is not None:
                dec["last_stand_down_reason"] = f"卖后重建{p['post_sell_rebuild_minutes']}分钟"

        if code in SIGNAL_OUTCOME_TRACKER:
            traces = SIGNAL_OUTCOME_TRACKER[code]
            now_ts = _now()
            for item in traces:
                elapsed_min = (now_ts - item["signal_time"]).total_seconds() / 60
                item["price_points"].append({
                    "ts": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "price": price,
                    "vwap": vwap,
                    "elapsed_min": round(elapsed_min, 2),
                })
                if elapsed_min >= 5 and not item["maturity_5m"]:
                    item["price_after_5m"] = price
                    item["vwap_after_5m"] = vwap
                    item["maturity_5m"] = True
                if elapsed_min >= 15 and not item["maturity_15m"]:
                    item["price_after_15m"] = price
                    item["vwap_after_15m"] = vwap
                    item["maturity_15m"] = True
                item["last_seen_price"] = price
                item["last_seen_vwap"] = vwap
                item["last_seen_time"] = now_ts.strftime("%Y-%m-%d %H:%M:%S")

        # V1.12: 最终阈值保护 - 确保ETF阈值不会被后续条件推高
        if is_etf:
            buy_threshold = min(buy_threshold, p.get("etf_threshold_cap", 38))
            sell_threshold = min(sell_threshold, p.get("etf_threshold_cap", 38))

        # ==================== V1.19: 弱势震荡/45度斜率/均线穿越对阈值的影响 ====================
        # 用户7月7日反馈：
        # 1. 平开/低开后全天均线下 → 弱势震荡，禁止买入，冲高保本即离场
        # 2. 45度下降 + 均线上下窜 → 震荡洗盘可低吸，也可高抛（华工科技/江丰电子）
        # 3. 45度下降 + 全天均线下 → 弱势阴跌，不可低吸，冲高即跑（特变电工/拓维信息）
        # 优先级：45度下降+均线上下窜 > 弱势震荡（前者覆盖后者）
        if is_steep_decline:
            if is_vwap_crossing and price_below_vwap_ratio < 0.80:
                # 45度下降 + 均线上下窜：震荡洗盘，反弹有力（如华工科技13:09/江丰电子13:17）
                # V1.19fix: 增加 price_below_vwap_ratio < 0.80 条件，避免特变电工/拓维信息型"全天均线下"被误判为可低吸
                buy_threshold -= 25  # V1.19: 从-20加大到-25，确保震荡低点能触发
                buy_score += 20      # V1.19: 从+15加大到+20，双重保障
                # V1.21fix: 强势震荡中不鼓励"45度洗盘可高抛"，避免过早卖出
                if not strong_chop:
                    sell_score += 8
                    sell_details.append({"指标": "45度洗盘可高抛", "当前": f"斜率{slope_pct_per_min:.2f}%/穿越{vwap_cross_count}次", "解读": "震荡区间内高抛也可做T", "加分": 0})
                buy_details.append({"指标": "45度洗盘可低吸", "当前": f"斜率{slope_pct_per_min:.2f}%/穿越{vwap_cross_count}次/均下{price_below_vwap_ratio*100:.0f}%", "解读": "45度下降但均线上下窜，反弹有力，可低吸（华工科技/江丰电子型）", "加分": 20})
            else:
                # 45度下降 + 全天均线下（或cross很少）：弱势阴跌（如特变电工/拓维信息）
                buy_threshold += 30  # V1.19: 从+25提高到+30，彻底封死买入
                buy_score = min(buy_score, 45)  # V1.19: 从50降到45，更严格
                sell_score += 18     # V1.19: 从+15提高到+18，鼓励冲高离场
                sell_threshold -= 10  # V1.19: 从-8降到-10，保本即走
                buy_details.append({"指标": "45度阴跌禁买", "当前": f"斜率{slope_pct_per_min:.2f}%/穿越{vwap_cross_count}次/均下{price_below_vwap_ratio*100:.0f}%", "解读": "45度下降且全天均线下方，主力无意拉升，不可低吸", "加分": 0})
                sell_details.append({"指标": "45度阴跌鼓励卖出", "当前": f"斜率{slope_pct_per_min:.2f}%", "解读": "弱势阴跌中冲高保本即跑", "加分": 0})
        elif is_weak_oscillation:
            # 弱势震荡（非45度下降+均线上下窜）：大幅抑制买入，降低卖出门槛
            if t_val < 1430:
                # V1.19: 14:30前弱势震荡中绝对禁止买入（用户明确反馈：尾盘前任何买入都不对）
                buy_threshold += 100
                buy_details.append({"指标": "弱势震荡14:30前禁买", "当前": f"均下{price_below_vwap_ratio*100:.0f}%", "解读": "全天弱势震荡，14:30前任何买入都不对", "加分": 0})
            else:
                buy_threshold += 35
                buy_score = min(buy_score, 45)
                buy_details.append({"指标": "弱势震荡抑制", "当前": f"均下{price_below_vwap_ratio*100:.0f}%/平低开", "解读": "全天弱势震荡，尾盘前不应买入", "加分": 0})
            # V1.19fix: 弱势震荡中，只要有利润空间就应卖出（保本离场）
            if sell_profit_space > 0:
                sell_threshold -= 20  # V1.19: 从-18加大到-20
                sell_details.append({"指标": "弱势震荡保本离场", "当前": f"均下{price_below_vwap_ratio*100:.0f}%/回吐空间+{sell_profit_space*100:.2f}%", "解读": "弱势震荡中价格回到均线上方，保本离场好时机", "加分": 0})
            else:
                sell_threshold -= 5
            # V1.19fix: 利润微薄时，进一步降低卖出阈值
            if (is_tight_profit or is_tight_gap) and sell_profit_space > 0:
                sell_threshold -= 15  # V1.19: 从-12加大到-15
                sell_details.append({"指标": "微利保本", "当前": f"利润{profit_pct*100:.1f}%", "解读": "弱势震荡+利润微薄，冲高必须保本离场", "加分": 0})
            sell_details.append({"指标": "弱势震荡鼓励卖出", "当前": f"均下{price_below_vwap_ratio*100:.0f}%", "解读": "弱势震荡中冲高即离场机会", "加分": 0})

        # V1.15: 阈值硬性上限保护 - 防止 redundant/stand_down/cycle_count 等叠加导致阈值异常到143+
        sell_threshold = min(sell_threshold, p.get("hard_sell_threshold_cap", 80))
        buy_threshold = min(buy_threshold, p.get("hard_buy_threshold_cap", 80))

        # V1.23fix: 强势上涨最终抑制（所有加分完成后）
        # 前置抑制后，后续加分（MA压力/成本保护/冲高背离等）可能抵消抑制效果
        # 最终抑制作为最后一道防线：强势上涨中完全封锁卖出信号
        if is_strong_uptrend:
            # 大幅提高卖出阈值，确保强势上涨中不触发卖出
            sell_threshold += 120
            sell_details.append({"指标": "强势上涨最终封锁", "当前": f"MA5>MA10>MA20/反弹>3%/价>VWAP", "解读": "强势上涨最终防线：前置抑制后后续加分可能抵消，最终封锁确保强势上升通道中不应卖出", "加分": 0})
        # 前置抑制后，后续加分（MA压力/成本保护/冲高背离等）可能抵消抑制效果
        # 最终抑制作为最后一道防线：强势上涨中完全封锁非必要的卖出信号
        if is_strong_uptrend and not is_optimal_sell and not is_deep_loss:
            # 大幅提高卖出阈值，确保强势上涨中不触发卖出（除非最优卖点或深套止损）
            sell_threshold += 120
            sell_details.append({"指标": "强势上涨最终封锁", "当前": f"MA5>MA10>MA20/反弹>3%/价>VWAP", "解读": "强势上涨最终防线：前置抑制后后续加分可能抵消，最终封锁确保不应卖出的强势形态被完全抑制", "加分": 0})

        # 【V1.15fix-5】下降趋势买入阈值提升：在ETF阈值保护后重新生效
        if prev_day_ret < -0.05:
            buy_threshold += 15
            if today_ret < -0.01:
                buy_threshold += 10

        # V1.15: 最优卖点加分（is_optimal_sell 已在前面定义）
        if is_optimal_sell:
            boost = p.get("optimal_sell_boost", 8)
            sell_score += boost
            sell_details.append({
                "指标": "最优卖点",
                "当前": f"range_pos={range_pos:.2f}/rsi={rsi:.1f}/bb_pct={bb_pct:.2f}/ret={today_ret*100:.1f}%",
                "解读": f"满足{optimal_sell_conditions}/4项最优卖点条件",
                "加分": boost,
            })
            diag["optimal_sell"] = True

        if not stand_down:
            buy_fast_path_gap = max(p["sell_fast_path_min_gap"], 18)
            buy_fast_path_protected = buy_score >= buy_threshold + p["buy_priority_margin"] and (buy_score - sell_score) >= p["buy_priority_margin"]
            # V1.15: 早盘防过早触发（09:30-9:40 首次卖出需今日涨幅>=2%，除非最优卖点）
            # V1.28fix: 大盘极弱(uni_down+score≤-30+冰点/清仓)时动态降低morning_no_sell_until
            if index_regime_status == "ok" and index_regime == "uni_down":
                _idx_score = float(daily_ctx.get("index_score", 0) or 0)
                _idx_temp = daily_ctx.get("index_temp_bucket", "neutral")
                if _idx_score <= -30 and (_idx_temp in ("freeze", "clear") or index_circuit_state in ("reduce", "clear")):
                    p = dict(p)
                    p["morning_no_sell_until"] = 930  # 09:30=等效不设保护
                    diag["uni_down_morning_guard_removed"] = True
                    diag["uni_down_idx_score"] = round(_idx_score, 1)
            morning_sell_guard = False
            if t_val < p.get("morning_no_sell_until", 940) and sell_today_count == 0 and today_ret < p.get("morning_no_sell_min_ret", 0.02):
                morning_sell_guard = True
                diag.setdefault("sell_block_reasons", []).append("morning_first_sell_guard")
            # V1.19fix: 深套股票在开盘反弹无力/假突破时，绕过早盘首次卖出保护
            if morning_sell_guard and is_deep_loss_stop_loss:
                morning_sell_guard = False
                diag["deep_loss_bypass_morning_guard"] = True
                if "morning_first_sell_guard" in diag.get("sell_block_reasons", []):
                    diag["sell_block_reasons"].remove("morning_first_sell_guard")
            # V1.27fix: 大幅低开直接绕过（>4% gap-down，深套直接止损，不等待反弹确认）
            if morning_sell_guard and is_deep_loss and t_val < p.get("morning_no_sell_until", 940) and today_ret < -0.04:
                morning_sell_guard = False
                diag["gap_down_bypass_morning_guard"] = True
                if "morning_first_sell_guard" in diag.get("sell_block_reasons", []):
                    diag["sell_block_reasons"].remove("morning_first_sell_guard")
            if can_sell and sell_score >= sell_threshold and (sell_score - buy_score) >= buy_fast_path_gap and not self._in_cooldown(code, "SELL_HIGH") and (not morning_sell_guard or is_optimal_sell):
                if morning_sell_guard and is_optimal_sell:
                    diag["optimal_sell_bypass_morning_guard"] = True
                if buy_fast_path_protected:
                    diag["preempted_by_sell_fast_path"] = True
                    diag["buy_block_reasons"].append("buy_priority_protection")
                else:
                    reasons = [d["指标"] for d in sell_details if d.get("加分", 0) > 0]
                    sell_details.append({"指标": "触发阈值", "当前": f"{sell_score:.0f}", "阈值": f">={sell_threshold}", "加分": 0})
                    diag["sell_candidate"] = True
                    diag["priority_path"] = "sell_observe_path"
                    action = "SELL_HIGH"
                    entry_kind = "sell_high"
                    sig = Signal(code, name, action, price, sell_score, reasons, sell_details, indicators, {
                    "side": "sell",
                    "threshold": sell_threshold,
                    "time_score": time_score,
                    "buy_score": buy_score,
                    "sell_score": sell_score,
                    "buy_factors": buy_factor_map,
                    "sell_factors": sell_factor_map,
                    "is_dead_water": is_dead_water,
                    "is_strong_trend": is_strong_trend,
                    "today_ret": today_ret,
                    "day_amplitude": day_amplitude,
                    "market_state": market_state,
                    "benchmark_state": benchmark_state,
                    "benchmark_gate": benchmark_gate,
                    "benchmark_reason": benchmark_reason,
                    "buy_limit_reason": buy_limit_reason,
                    "hold_qty": hold_qty,
                    "net_qty": net_qty,
                    "cycle_count": cycle_count,
                    "entry_kind": entry_kind,
                    "sell_stage": "observe",
                    "sell_qty_pct": diag.get("pressure_support", {}).get("sell_qty_pct", 100),
                }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
                # V1.21: 高抛后进入等待低吸状态（观察路径也设置）
                self.awaiting_buyback[code] = {
                    "sell_price": price,
                    "sell_ts": _now(),
                    "qty": hold_qty,
                    "expires": _now() + timedelta(minutes=p.get("awaiting_buyback_ttl_minutes", 120)),
                    "sell_vwap": vwap,
                }
            # V1.14: 开盘急跌旁路买入 — 开盘后5分钟内，跌幅>2%，触及支撑位，直接买入
            elif is_open_dip_support and can_buy_more and not self._in_cooldown(code, "BUY_LOW") and (not post_sell_block_active or post_sell_rebuild_allowed):
                diag["buy_candidate"] = True
                diag["priority_path"] = "buy_open_dip_path"
                reasons = ["开盘急跌旁路"]
                if nearest_support:
                    reasons.append(f"触及{nearest_support[0]}({nearest_support[1]:.2f})")
                # 旁路买入评分：固定60分（确保触发）
                bypass_score = 60
                buy_details.append({"指标": "开盘急跌旁路", "当前": f"跌{today_ret*100:.1f}%", "解读": open_dip_reason, "加分": 0})
                buy_details.append({"指标": "触发阈值", "当前": f"{bypass_score:.0f}", "阈值": "旁路", "加分": 0})
                sig = Signal(code, name, "BUY_LOW", price, bypass_score, reasons, buy_details, indicators, {
                    "side": "buy",
                    "threshold": buy_threshold,
                    "time_score": time_score,
                    "buy_score": bypass_score,
                    "sell_score": sell_score,
                    "buy_factors": buy_factor_map,
                    "sell_factors": sell_factor_map,
                    "is_dead_water": is_dead_water,
                    "is_strong_trend": is_strong_trend,
                    "today_ret": today_ret,
                    "day_amplitude": day_amplitude,
                    "market_state": market_state,
                    "benchmark_state": benchmark_state,
                    "benchmark_gate": benchmark_gate,
                    "benchmark_reason": benchmark_reason,
                    "buy_limit_reason": buy_limit_reason,
                    "hold_qty": hold_qty,
                    "net_qty": net_qty,
                    "cycle_count": cycle_count,
                    "entry_kind": "open_dip_support",
                    "open_dip_support": True,
                    "nearest_support": indicators.get("nearest_support_name", ""),
                    "support_level": indicators.get("nearest_support_level", 0),
                }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
                # V1.21: 买入后清除等待低吸状态
                if code in self.awaiting_buyback:
                    del self.awaiting_buyback[code]
            # V1.25: 早盘预警门控 — Level 2禁止买入，Level 1只做减仓
            # V1.26: 反T模式下反转逻辑：早盘弱势时鼓励卖出，买入仍然受限
            elif effective_alert >= 2:
                if is_short_mode:
                    # 反T+早盘弱势 = 绝佳卖出时机，降低卖出门槛
                    sell_threshold -= 15
                    sell_score += 15
                    sell_details.append({"指标": "🚨反T早盘弱势卖出", "当前": f"Level {effective_alert}", "解读": "反T模式：早盘弱势是最佳卖出时机，降低卖出门槛", "加分": 15})
                    buy_threshold = 999  # 买入仍然禁止（等深跌接回）
                else:
                    diag["buy_candidate"] = False
                    diag["morning_alert_block"] = True
                    buy_details.append({"指标": "🚨早盘预警禁止买入", "当前": f"Level {effective_alert}", "解读": "早盘触发单边下行预警，全天禁止买入做T", "加分": 0})
                    buy_score = 0
                    buy_threshold = 999
            elif effective_alert == 1:
                if is_short_mode:
                    # 反T+早盘谨慎 = 鼓励卖出
                    sell_threshold -= 8
                    sell_score += 8
                    sell_details.append({"指标": "⚠️反T早盘弱势卖出", "当前": f"Level {effective_alert}", "解读": "反T模式：早盘弱势，优先卖出", "加分": 8})
                    buy_threshold += 25
                else:
                    diag["buy_candidate"] = False
                    diag["morning_alert_caution"] = True
                    buy_details.append({"指标": "⚠️早盘预警谨慎", "当前": f"Level {effective_alert}", "解读": "早盘弱势，只做减仓不做加仓", "加分": 0})
                    buy_threshold += 25
            elif can_buy_more and buy_score >= buy_threshold and not self._in_cooldown(code, "BUY_LOW") and (not post_sell_block_active or post_sell_rebuild_allowed):
                diag["buy_candidate"] = True
                reasons = [d["指标"] for d in buy_details if d.get("加分", 0) > 0]
                if benchmark_gate == "strong" and is_strong_trend and price >= prev_high and vol_ratio >= p["vol_ratio_confirm"]:
                    reasons = list(dict.fromkeys(reasons + ["强势突破"]))
                buy_time_ready = current_minute >= min_trade_minute
                buy_confirm_ts = self.last_signal_state.get(code, {}).get("ts")
                buy_confirm_elapsed = (_now() - buy_confirm_ts).total_seconds() if isinstance(buy_confirm_ts, datetime) else 9999
                buy_confirm_floor = buy_confirm_min_seconds if not post_sell_rebuild_allowed else max(20, buy_confirm_min_seconds - 30)
                buy_confirm_ready = buy_confirm_elapsed >= max(buy_confirm_floor, 90 if not post_sell_rebuild_allowed else 45)
                buy_momentum_ok = (mom5 > -0.004 or is_strong_pullback or market_state == "trend_up" or (is_steep_decline and is_vwap_crossing)) if buy_needs_momentum else True
                # V1.22: EMA确认需要大阳线反包（防止开盘急跌时EMA假性转强）
                buy_ema_ok = (ema_spread > prev_ema_spread or ema_spread > -0.0005 or daily_pullback_support or is_strong_bullish_reversal or (is_steep_decline and is_vwap_crossing)) if buy_needs_ema else True
                buy_volume_ok = (vol_ratio >= 1.0) if buy_needs_volume else True
                buy_gap_floor = max(2, buy_rebound_min_score_gap - 1)
                if post_sell_rebuild_allowed:
                    buy_gap_floor = max(0, buy_gap_floor - p["post_sell_rebuild_relax_gap"] - 2)
                buy_gap_ok = (buy_score - sell_score) >= buy_gap_floor
                buy_detail_need = buy_confirm_min_factors + 1
                if post_sell_rebuild_allowed:
                    buy_detail_need = max(1, buy_detail_need - p["post_sell_rebuild_relax_factors"] - 1)
                buy_detail_count_ok = len(buy_details) >= buy_detail_need
                # V1.14fix: 当价格低于均价且14:30前，绝对禁止买入（无论market_state或is_strong_pullback）
                # V1.17修正: 当5分钟量能反转信号确认时，允许在14:30前价格低于VWAP时买入
                # V1.18fix: 当均线上下窜（震荡洗盘）时，允许在14:30前价格低于VWAP时买入
                # V1.19fix: 弱势震荡/阴跌时，14:30前绝对禁止买入（用户明确反馈：尾盘前任何买入都不对）
                # 缩量止跌+放量反攻是明确的低吸信号，不应被价格<VWAP阻挡
                # V1.21fix: 高抛后等待低吸时，强制允许买入（即使价格低于VWAP）
                # V1.22fix: 开盘急跌窗口（前15分钟）无大阳线反包时，绝对禁止买入（防止买在半山腰）
                if ab_active:
                    buy_price_ok = True
                elif price < vwap and t_val < 1430 and not is_strong_bullish_reversal and not is_vwap_crossing:
                    buy_price_ok = False
                elif is_weak_oscillation and t_val < 1430:
                    # V1.19: 弱势震荡中，14:30前禁止买入
                    buy_price_ok = False
                elif is_steep_decline and not is_vwap_crossing:
                    # V1.19: 45度阴跌无穿越，禁止买入
                    buy_price_ok = False
                else:
                    buy_price_ok = (price <= vwap and mom5 <= 0) or is_strong_pullback or market_state == "trend_up" or is_strong_bullish_reversal

                # V1.13: 15分钟级别买入确认 — 下跌动能仍在加速时暂缓买入
                # V1.17修正: 当5分钟量能反转确认时，允许15分钟动能未完全衰竭时买入
                # V1.18fix: 当45度下降+均线上下窜（震荡洗盘）时，放宽15分钟动能确认
                # V1.22fix: 大阳线反包确认时，15分钟动能确认完全放宽
                buy_15m_ok = True
                if not df_15min.empty and len(df_15min) >= PARAMS.get("min_15min_bars", 3) and not is_strong_bullish_reversal and not (is_steep_decline and is_vwap_crossing):
                    # 15分钟MACD仍在加速下跌（负区且柱状体继续扩大），暂缓买入
                    if macd_hist_15m < prev_macd_hist_15m and macd_hist_15m < PARAMS.get("macd_15m_exhaustion_threshold", -0.001):
                        buy_15m_ok = False
                    # 15分钟放量大跌（动能未衰竭），暂缓买入
                    elif mom2_15m < -0.02 and vol_ratio_15m > 1.5:
                        buy_15m_ok = False

                # V1.14: 5分钟级别买入确认 — 量能缩量 + 企稳反转
                # V1.22fix: 开盘急跌无大阳线反包时，5分钟确认严格把关
                buy_5m_ok = True
                # V1.21fix: 高抛后等待低吸时，放宽5分钟确认条件
                if not ab_active and not df_5min.empty and len(df_5min) >= 3:
                    # 5分钟量能未缩量（继续放量下跌），暂缓买入
                    if vol_ratio_5m > 1.2 and mom2_5m < -0.01:
                        buy_5m_ok = False
                    # 5分钟MACD仍在加速下跌，暂缓买入
                    elif macd_hist_5m < prev_macd_hist_5m and macd_hist_5m < -0.001:
                        buy_5m_ok = False
                    # 5分钟低点未抬高且未止跌，暂缓买入
                    elif not is_low_rising_5m and not is_stop_falling_5m and mom2_5m < -0.005:
                        buy_5m_ok = False
                    # V1.22: 开盘急跌窗口（前15分钟）且当前5分钟K线无大阳线反包，强制禁止买入
                    elif current_idx_b <= p.get("open_dip_max_mins", 15) and not is_strong_bullish_reversal and mom2_5m < 0:
                        buy_5m_ok = False
                elif ab_active:
                    # V1.21: 高抛后低吸时，5分钟条件完全放宽
                    buy_5m_ok = True
                    # 否则 buy_5m_ok 保持 True
                
                # 5分钟企稳反转加分
                if buy_5m_ok and is_low_rising_5m and is_stop_falling_5m and vol_ratio_5m < 0.9:
                    buy_score += 5
                    buy_details.append({"指标": "5分企稳反转", "当前": f"量缩{vol_ratio_5m:.1f}倍/低点抬高", "解读": "5分钟级别缩量企稳，低吸确认", "加分": 5})

                sell_time_ready = current_minute >= min_trade_minute
                sell_confirm_ts = self.last_signal_state.get(code, {}).get("ts")
                sell_confirm_elapsed = (_now() - sell_confirm_ts).total_seconds() if isinstance(sell_confirm_ts, datetime) else 9999
                sell_confirm_ready = sell_confirm_elapsed >= max(sell_confirm_min_seconds, 45)
                sell_detail_count_ok = len(sell_details) >= sell_confirm_min_factors + 1
                sell_momentum_ok = (mom5 < -0.004 or market_state == "trend_down") if sell_needs_momentum else True
                sell_ema_ok = ((ema_spread < prev_ema_spread and ema_spread < -0.001) or (ema_spread < -0.002)) if sell_needs_ema else True
                sell_volume_ok = (vol_ratio >= p["vol_ratio_confirm"] + 0.3) if sell_needs_volume else True
                buy_support_count = _buy_soft_support_count(buy_momentum_ok, buy_ema_ok, buy_volume_ok, buy_price_ok, buy_gap_ok, buy_detail_count_ok, buy_time_ready, buy_15m_ok, buy_5m_ok)
                low_buy_threshold = buy_threshold - p["buy_soft_margin"]
                low_buy_threshold -= int(base_memory.get("buy_low_threshold_adj", 0) or 0)
                buy_candidate_preheat = daily_pullback_support and price <= vwap and mom5 > -0.004 and buy_support_count >= max(3, buy_detail_need - 2) and buy_score >= low_buy_threshold - 2
                if buy_candidate_preheat:
                    diag["buy_candidate"] = True
                    diag["priority_path"] = "buy_soft_path"
                    diag.setdefault("buy_block_reasons", []).append("buy_preheat")
                buy_soft_ready = buy_score >= max(low_buy_threshold, buy_threshold - 4) and buy_support_count >= max(4, buy_detail_need - 1) and (buy_confirm_ready or current_minute >= min_trade_minute) and buy_15m_ok and buy_5m_ok
                # V1.21: 记录卖出条件状态用于调试
                diag["sell_conditions"] = {
                    "sell_score": sell_score,
                    "sell_threshold": sell_threshold,
                    "sell_fast_path_gap": buy_fast_path_gap,
                    "sell_score_minus_buy_score": sell_score - buy_score,
                    "in_cooldown": self._in_cooldown(code, "SELL_HIGH"),
                    "morning_sell_guard": morning_sell_guard,
                    "is_optimal_sell": is_optimal_sell,
                    "stand_down": stand_down,
                    "market_state": market_state,
                }
                diag["buy_conditions"] = {
                    "ab_active": ab_active,
                    "buy_time_ready": buy_time_ready,
                    "buy_confirm_ready": buy_confirm_ready,
                    "buy_momentum_ok": buy_momentum_ok,
                    "buy_ema_ok": buy_ema_ok,
                    "buy_volume_ok": buy_volume_ok,
                    "buy_gap_ok": buy_gap_ok,
                    "buy_detail_count_ok": buy_detail_count_ok,
                    "buy_price_ok": buy_price_ok,
                    "buy_15m_ok": buy_15m_ok,
                    "buy_5m_ok": buy_5m_ok,
                    "daily_buy_t_ok": daily_buy_t_ok,
                    "buy_score": buy_score,
                    "buy_threshold": buy_threshold,
                    "buy_gap_floor": buy_gap_floor,
                }
                # V1.24: 中文在线破位日禁止抄底 — 当日跌破前低1.5%时彻底阻断买入
                if p.get("breakdown_buy_block", False) and daily_breakdown_risk:
                    breakdown_gap = daily_ctx.get("daily_breakdown_gap", 0.0) or 0.0
                    if breakdown_gap <= -p.get("breakdown_gap_threshold", 0.015):
                        buy_threshold += 100
                        buy_details.append({
                            "指标": "破位日禁止抄底",
                            "当前": f"跌破前低{abs(breakdown_gap)*100:.1f}%",
                            "解读": "日线已跌破前低，破位确认，严禁任何抄底行为",
                            "加分": 0,
                        })
                        diag.setdefault("buy_block_reasons", []).append("日线破位禁止抄底")

                # V1.24: 每日交易次数上限 — 防止中文在线过度交易被手续费侵蚀
                daily_trade_limit = p.get("daily_trade_limit", 0)
                if daily_trade_limit > 0:
                    today_complete_t = min(buy_today_count, sell_today_count)
                    if today_complete_t >= daily_trade_limit:
                        buy_threshold += 100
                        sell_threshold += 100
                        diag.setdefault("buy_block_reasons", []).append(f"日交易上限{daily_trade_limit}次")
                        diag.setdefault("sell_block_reasons", []).append(f"日交易上限{daily_trade_limit}次")

                if buy_time_ready and buy_confirm_ready and buy_momentum_ok and buy_ema_ok and buy_volume_ok and buy_gap_ok and buy_detail_count_ok and buy_price_ok and buy_15m_ok and buy_5m_ok and daily_buy_t_ok:
                    diag["priority_path"] = "buy_path"
                    reasons = [d["指标"] for d in buy_details if d.get("加分", 0) > 0]
                    if post_sell_rebuild_allowed:
                        reasons = list(dict.fromkeys(reasons + ["卖后重建"]))
                    if daily_ma5_state == "above_ma5_trend":
                        reasons = list(dict.fromkeys(reasons + ["MA5上行顺势T"]))
                    act = "ADD_POS" if ("主升浪回踩" in reasons or "强势突破" in reasons) else "BUY_LOW"
                    buy_details.append({"指标": "触发阈值", "当前": f"{buy_score:.0f}", "阈值": f">={buy_threshold}", "加分": 0})
                    sig = Signal(code, name, act, price, buy_score, reasons, buy_details, indicators, {
                        "side": "buy",
                        "threshold": buy_threshold,
                        "time_score": time_score,
                        "buy_score": buy_score,
                        "sell_score": sell_score,
                        "buy_factors": buy_factor_map,
                        "sell_factors": sell_factor_map,
                        "is_dead_water": is_dead_water,
                        "is_strong_trend": is_strong_trend,
                        "today_ret": today_ret,
                        "day_amplitude": day_amplitude,
                        "market_state": market_state,
                        "benchmark_state": benchmark_state,
                        "benchmark_gate": benchmark_gate,
                        "benchmark_reason": benchmark_reason,
                        "buy_limit_reason": buy_limit_reason,
                        "hold_qty": hold_qty,
                        "net_qty": net_qty,
                        "cycle_count": cycle_count,
                        "entry_kind": "strong_breakout" if act == "ADD_POS" and "强势突破" in reasons else ("pullback" if act == "ADD_POS" else "low_buy"),
                    }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
                    # V1.21: 买入后清除等待低吸状态
                    if code in self.awaiting_buyback:
                        del self.awaiting_buyback[code]
                elif can_buy_more and buy_soft_ready and not self._in_cooldown(code, "BUY_LOW") and (not post_sell_block_active or post_sell_rebuild_allowed):
                    # V1.8fix: 当 daily_ma5 不可用时，不再直接阻断，而是检查降级条件
                    if daily_ma5_state == "below_ma5_weak":
                        diag.setdefault("buy_block_reasons", []).append("below_daily_ma5")
                    elif daily_ma5_state == "unknown" and not daily_buy_t_ok:
                        diag.setdefault("buy_block_reasons", []).append("daily_ma5_unavailable")
                    elif daily_ma5_state == "above_ma5_trend" and not (is_strong_pullback or market_state == "trend_up"):
                        diag.setdefault("buy_block_reasons", []).append("above_ma5_trend_soft_buy_blocked")
                    else:
                        diag["buy_candidate"] = True
                        diag["priority_path"] = "buy_soft_path"
                        reasons = [d["指标"] for d in buy_details if d.get("加分", 0) > 0]
                        if post_sell_rebuild_allowed:
                            reasons = list(dict.fromkeys(reasons + ["卖后重建"]))
                        if daily_ma5_state == "above_ma5_trend":
                            reasons = list(dict.fromkeys(reasons + ["MA5上行顺势T"]))
                        buy_details.append({"指标": "软确认阈值", "当前": f"{buy_score:.0f}", "阈值": f">={buy_threshold - p['buy_soft_margin']}", "加分": 0})
                        sig = Signal(code, name, "BUY_LOW", price, buy_score, reasons, buy_details, indicators, {
                            "side": "buy",
                            "threshold": buy_threshold,
                            "time_score": time_score,
                            "buy_score": buy_score,
                            "sell_score": sell_score,
                            "buy_factors": buy_factor_map,
                            "sell_factors": sell_factor_map,
                            "is_dead_water": is_dead_water,
                            "is_strong_trend": is_strong_trend,
                            "today_ret": today_ret,
                            "day_amplitude": day_amplitude,
                            "market_state": market_state,
                            "benchmark_state": benchmark_state,
                            "benchmark_gate": benchmark_gate,
                            "benchmark_reason": benchmark_reason,
                            "buy_limit_reason": buy_limit_reason,
                            "hold_qty": hold_qty,
                            "net_qty": net_qty,
                            "cycle_count": cycle_count,
                            "entry_kind": "soft_buy" if post_sell_rebuild_allowed else "low_buy",
                            "soft_buy": True,
                            "soft_support_count": buy_support_count,
                            "low_buy_threshold": low_buy_threshold,
                            "preopen_stage": "open" if current_minute < 945 else ("eod" if current_minute >= 1455 else "intraday"),
                        }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
                        # V1.21: 买入后清除等待低吸状态
                        if code in self.awaiting_buyback:
                            del self.awaiting_buyback[code]
                elif can_sell and sell_score >= sell_threshold and sell_time_ready and sell_confirm_ready and sell_detail_count_ok and sell_momentum_ok and sell_ema_ok and sell_volume_ok and (not morning_sell_guard or is_optimal_sell):
                    if morning_sell_guard and is_optimal_sell:
                        diag["optimal_sell_bypass_morning_guard"] = True
                    diag["sell_candidate"] = True
                    diag["priority_path"] = "sell_confirm_path"
                    reasons = [d["指标"] for d in sell_details if d.get("加分", 0) > 0]
                    sell_details.append({"指标": "触发阈值", "当前": f"{sell_score:.0f}", "阈值": f">={sell_threshold}", "加分": 0})
                    sig = Signal(code, name, "SELL_HIGH", price, sell_score, reasons, sell_details, indicators, {
                        "side": "sell",
                        "threshold": sell_threshold,
                        "time_score": time_score,
                        "buy_score": buy_score,
                        "sell_score": sell_score,
                        "buy_factors": buy_factor_map,
                        "sell_factors": sell_factor_map,
                        "is_dead_water": is_dead_water,
                        "is_strong_trend": is_strong_trend,
                        "today_ret": today_ret,
                        "day_amplitude": day_amplitude,
                        "market_state": market_state,
                        "buy_limit_reason": buy_limit_reason,
                        "hold_qty": hold_qty,
                        "net_qty": net_qty,
                        "cycle_count": cycle_count,
                        "sell_stage": "execute",
                        "sell_qty_pct": diag.get("pressure_support", {}).get("sell_qty_pct", 100),
                    }, cycle_id=code, cycle_action_count=cycle_count, hold_qty=hold_qty)
                    # V1.21: 高抛后进入等待低吸状态
                    self.awaiting_buyback[code] = {
                        "sell_price": price,
                        "sell_ts": _now(),
                        "qty": hold_qty,
                        "expires": _now() + timedelta(minutes=p.get("awaiting_buyback_ttl_minutes", 120)),
                        "sell_vwap": vwap,
                    }

        dec = DAILY_DECISION_STATS.get(code)
        if dec is not None:
            dec["last_market_state"] = market_state
            dec["last_buy_limit_reason"] = buy_limit_reason
            dec["last_stand_down_reason"] = stand_down_reason if stand_down else ""

        if sig is None:
            if diag["buy_candidate"]:
                if not buy_time_ready:
                    diag["buy_block_reasons"].append("buy_time_not_ready")
                if not buy_confirm_ready:
                    diag["buy_block_reasons"].append("buy_confirm_wait")
                if not buy_momentum_ok:
                    diag["buy_block_reasons"].append("buy_momentum_fail")
                if not buy_ema_ok:
                    diag["buy_block_reasons"].append("buy_ema_fail")
                if not buy_volume_ok:
                    diag["buy_block_reasons"].append("buy_volume_fail")
                if not buy_gap_ok:
                    diag["buy_block_reasons"].append("buy_gap_fail")
                if not buy_detail_count_ok:
                    diag["buy_block_reasons"].append("buy_detail_fail")
                if not buy_price_ok:
                    diag["buy_block_reasons"].append("buy_price_fail")
                if not buy_15m_ok:
                    diag["buy_block_reasons"].append("buy_15m_kinetic_not_exhausted")
                if not can_buy_more:
                    diag["buy_block_reasons"].append("buy_limit")
                if self._in_cooldown(code, "BUY_LOW"):
                    diag["buy_block_reasons"].append("buy_cooldown")
                if post_sell_block_active and not post_sell_rebuild_allowed:
                    diag["buy_block_reasons"].append("post_sell_block")
                if 'buy_soft_ready' in locals() and buy_soft_ready:
                    diag["buy_block_reasons"].append("buy_soft_ready")
                    diag["priority_path"] = diag.get("priority_path") or "buy_soft_path"
            if diag["sell_candidate"] and sig is None:
                if not can_sell:
                    diag["sell_block_reasons"].append("sell_no_position")
                if not sell_time_ready:
                    diag["sell_block_reasons"].append("sell_time_not_ready")
                if not sell_confirm_ready:
                    diag["sell_block_reasons"].append("sell_confirm_wait")
                if not sell_detail_count_ok:
                    diag["sell_block_reasons"].append("sell_detail_fail")
                if not sell_momentum_ok:
                    diag["sell_block_reasons"].append("sell_momentum_fail")
                if not sell_ema_ok:
                    diag["sell_block_reasons"].append("sell_ema_fail")
                if not sell_volume_ok:
                    diag["sell_block_reasons"].append("sell_volume_fail")
                if self._in_cooldown(code, "SELL_HIGH"):
                    diag["sell_block_reasons"].append("sell_cooldown")
        decision = sig.action if sig else "HOLD"
        decision_reason = " + ".join(sig.reasons) if sig and sig.reasons else (stand_down_reason if stand_down else "")
        # V1.18fix: 调试信息，记录最终 threshold 和 score
        diag["final_buy_score"] = buy_score
        diag["final_buy_threshold"] = buy_threshold
        diag["final_sell_score"] = sell_score
        diag["final_sell_threshold"] = sell_threshold
        _append_jsonl(_trace_path("decision_trace"), {
            "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": code,
            "name": name,
            "price": price,
            "vwap": vwap,
            "rsi": rsi,
            "bb_pct": bb_pct,
            "macd_hist": macd_hist,
            "ema_spread": ema_spread,
            "range_pos": range_pos,
            "vol_ratio": vol_ratio,
            "mom5": mom5,
            "lower_shadow": lower_shadow,
            "upper_shadow": upper_shadow,
            "day_amplitude": day_amplitude,
            "today_ret": today_ret,
            "prev_high": prev_high,
            "is_strong_trend": is_strong_trend,
            "is_strong_pullback": is_strong_pullback,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "market_state": market_state,
            "benchmark_code": benchmark.get("benchmark_code", ""),
            "benchmark_state": benchmark_state,
            "benchmark_gate": benchmark_gate,
            "daily_status": daily_ctx.get("daily_status", "unknown"),
            "daily_gate": daily_ctx.get("daily_gate", "neutral"),
            "daily_trend_bg": daily_ctx.get("daily_trend_bg", "unknown"),
            "daily_ma5": daily_ctx.get("daily_ma5", 0.0),
            "daily_ma5_slope": daily_ctx.get("daily_ma5_slope", 0.0),
            "daily_above_ma5": daily_ctx.get("daily_above_ma5", False),
            "daily_ma5_gap": daily_ctx.get("daily_ma5_gap", 0.0),
            "daily_ma5_state": daily_ctx.get("daily_ma5_state", "unknown"),
            "daily_buy_t_ok": daily_buy_t_ok,
            "daily_sell_t_preferred": daily_sell_t_preferred,
            "daily_buy_t_relaxed": daily_buy_t_relaxed,
            "daily_support_name": daily_ctx.get("daily_support_name", ""),
            "daily_support_gap": daily_ctx.get("daily_support_gap", 0.0),
            "daily_breakdown_risk": daily_ctx.get("daily_breakdown_risk", False),
            "daily_overheated": daily_ctx.get("daily_overheated", False),
            "daily_ma10": daily_ctx.get("daily_ma10", 0.0),
            "daily_ma20": daily_ctx.get("daily_ma20", 0.0),
            "daily_ma30": daily_ctx.get("daily_ma30", 0.0),
            "daily_ma60": daily_ctx.get("daily_ma60", 0.0),
            "daily_ma120": daily_ctx.get("daily_ma120", 0.0),
            "daily_ma150": daily_ctx.get("daily_ma150", 0.0),
            "daily_ma180": daily_ctx.get("daily_ma180", 0.0),
            "daily_ma250": daily_ctx.get("daily_ma250", 0.0),
            "daily_ma365": daily_ctx.get("daily_ma365", 0.0),
            "hold_qty": hold_qty,
            "net_qty": net_qty,
            "cycle_count": cycle_count,
            "minute_status": MINUTE_FETCH_STATUS.get(code, "unknown"),
            "minute_detail": MINUTE_FETCH_DETAIL.get(code, ""),
            "decision": decision,
            "decision_reason": decision_reason,
            "buy_factors": buy_factor_map,
            "sell_factors": sell_factor_map,
            "buy_detail_count": len(buy_details),
            "sell_detail_count": len(sell_details),
            "stand_down_reason": stand_down_reason if stand_down else "",
            "cooldown_reason": "卖后重建" if post_sell_block_active else "",
            "post_sell_block": post_sell_block_active,
            "is_dead_water": is_dead_water,
            "priority_path": diag.get("priority_path", "hold"),
            "buy_candidate": diag.get("buy_candidate", False),
            "buy_candidate_preheat": bool(locals().get("buy_candidate_preheat", False)),
            "sell_candidate": diag.get("sell_candidate", False),
            "buy_block_reasons": diag.get("buy_block_reasons", []),
            "sell_block_reasons": diag.get("sell_block_reasons", []),
            "preempted_by_sell_fast_path": diag.get("preempted_by_sell_fast_path", False),
            "soft_buy_ready": bool(locals().get("buy_soft_ready", False)),
            "soft_buy_support_count": int(locals().get("buy_support_count", 0) or 0),
            # V1.15: 新增诊断字段
            "is_optimal_sell": diag.get("optimal_sell", False),
            "morning_sell_guard": bool(locals().get("morning_sell_guard", False)),
            "upper_shadow_approx": diag.get("upper_shadow_approx", False),
            "optimal_sell_bypass_redundant": diag.get("optimal_sell_bypass_redundant", False),
            "optimal_sell_bypass_morning_guard": diag.get("optimal_sell_bypass_morning_guard", False),
        })
        if sig is None and (buy_score >= buy_threshold - 4 or sell_score >= sell_threshold - 4):
            shadow_side = "buy" if buy_score >= sell_score else "sell"
            _append_jsonl(_trace_path("shadow_signals"), {
                "scan_time": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "code": code,
                "name": name,
                "buy_score": buy_score,
                "sell_score": sell_score,
                "buy_threshold": buy_threshold,
                "sell_threshold": sell_threshold,
                "current_price": price,
                "vwap": vwap,
                "distance_to_buy_threshold": buy_threshold - buy_score,
                "distance_to_sell_threshold": sell_threshold - sell_score,
                "would_trigger_new_params": False,
                "would_trigger_old_params": bool(buy_score >= (buy_threshold - 4) or sell_score >= (sell_threshold - 4)),
                "best_signal_type": shadow_side,
                "best_signal_score": max(buy_score, sell_score),
                "miss_reason": decision_reason or "接近阈值但未触发",
                "market_state": market_state,
                "benchmark_gate": benchmark_gate,
                "daily_gate": daily_ctx.get("daily_gate", "neutral"),
                "daily_trend_bg": daily_ctx.get("daily_trend_bg", "unknown"),
                "daily_status": daily_ctx.get("daily_status", "unknown"),
                "daily_ma5_state": daily_ctx.get("daily_ma5_state", "unknown"),
                "daily_buy_t_ok": daily_buy_t_ok,
                "daily_buy_t_relaxed": daily_buy_t_relaxed,
                "daily_sell_t_preferred": daily_sell_t_preferred,
                "minute_status": MINUTE_FETCH_STATUS.get(code, "unknown"),
                "buy_factor_count": len(buy_details),
                "sell_factor_count": len(sell_details),
            })
            # V1.11: 记录信号错过原因日志
            if _log_enhancer and decision_reason:
                miss_signal_type = "BUY_LOW" if shadow_side == "buy" else "SELL_HIGH"
                miss_score = buy_score if shadow_side == "buy" else sell_score
                miss_threshold = buy_threshold if shadow_side == "buy" else sell_threshold
                detail_reasons = [d.get("指标", "") for d in (buy_details if shadow_side == "buy" else sell_details) if d.get("加分", 0) > 0]
                _log_enhancer.log_missed_signal(
                    code=code, name=name, signal_type=miss_signal_type,
                    price=price, vwap=vwap, score=miss_score, threshold=miss_threshold,
                    miss_reason=decision_reason, detail_reasons=detail_reasons
                )
        if code in SIGNAL_OUTCOME_TRACKER:
            out_path = _result_trace_path()
            for item in SIGNAL_OUTCOME_TRACKER[code]:
                item["final_time"] = _now().strftime("%Y-%m-%d %H:%M:%S")
                item["price_now"] = price
                item["vwap_now"] = vwap
                item["price_after_5m"] = item.get("price_after_5m", price)
                item["price_after_15m"] = item.get("price_after_15m", price)
                signal_price = float(item.get("signal_price", 0) or 0)
                price_now = float(item.get("price_now", price) or price)
                vwap_now = float(item.get("vwap_now", vwap) or vwap)
                price_5m = float(item.get("price_after_5m", price_now) or price_now)
                price_15m = float(item.get("price_after_15m", price_now) or price_now)
                item["ret_5m_pct"] = ((price_5m - signal_price) / signal_price * 100) if signal_price else 0.0
                item["ret_15m_pct"] = ((price_15m - signal_price) / signal_price * 100) if signal_price else 0.0
                item["win_5m"] = False
                item["win_15m"] = False
                if item.get("action") in {"BUY_LOW", "ADD_POS"}:
                    item["win_5m"] = price_5m >= signal_price * 1.001 if signal_price else False
                    item["win_15m"] = price_15m >= signal_price * 1.0015 if signal_price else False
                    if price_now >= signal_price * 1.003:
                        item["final_classification"] = "correct"
                    elif price_now <= signal_price * 0.997:
                        item["final_classification"] = "buy_early"
                    elif price_now > signal_price and price_now < signal_price * 1.003:
                        item["final_classification"] = "buy_validating"
                    else:
                        item["final_classification"] = "hold_pending"
                elif item.get("action") in {"SELL_HIGH", "PANIC_SELL"}:
                    item["win_5m"] = price_5m <= signal_price * 0.999 if signal_price else False
                    item["win_15m"] = price_15m <= signal_price * 0.9985 if signal_price else False
                    if price_now <= signal_price * 0.997:
                        item["final_classification"] = "correct"
                    elif price_now >= signal_price * 1.008:
                        item["final_classification"] = "sell_early"
                    elif price_now < signal_price and price_now > signal_price * 0.995:
                        item["final_classification"] = "sell_validating"
                    else:
                        item["final_classification"] = "hold_pending"
                else:
                    item["final_classification"] = "correct"
                item["maturity_5m"] = bool(item.get("maturity_5m", False))
                item["maturity_15m"] = bool(item.get("maturity_15m", False))
                item["final_vwap_gap_pct"] = ((price_now - vwap_now) / vwap_now * 100) if vwap_now else 0.0
                _append_jsonl(out_path, item)
            SIGNAL_OUTCOME_TRACKER[code] = []
        return buy_score, sell_score, sig

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
