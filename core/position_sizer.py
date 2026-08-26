# -*- coding: utf-8 -*-
"""
position_sizer.py — 动态仓位管理器（V1.14 新架构）

功能：
1. 根据 market_regime 决定卖出/买入股数
2. 支持"高抛低吸组合拳"（卖后优先接回）
3. 支持重压场景下的清仓/减仓策略
4. 支持常规场景下的分批交易

集成方式：
  在 main.py 的模块加载顺序中，market_regime.py 之后加载本模块
  通过共享命名空间中的 PositionSizer 类调用
"""

import logging
from typing import Dict, Any, Optional
from market_regime import MarketRegime, should_clear_all, should_reduce

log = logging.getLogger("做T助手")

# fix P0-9(B4): 全量持仓引用（由 main.py 注入），供单股上限合并 A/B 双账户判定
_ALL_HOLDINGS_REF: Optional[dict] = None


def set_all_holdings(holdings: dict) -> None:
    """fix P0-9(B4): 注入全量持仓引用，_check_position_limit 按底层代码合并 A/B 市值。"""
    global _ALL_HOLDINGS_REF
    _ALL_HOLDINGS_REF = holdings or {}


class PositionSizer:
    """动态仓位管理器 + V1.27 日线止损 + 持仓上限"""

    def __init__(self, params: dict = None, virtual_trades: dict = None):
        """
        参数:
            params: 全局参数（从 config.py 的 PARAMS 传入）
            virtual_trades: 虚拟交易记录（从 signal_engine 的 VIRTUAL_TRADES 传入）
        """
        self.params = params or {}
        self.virtual_trades = virtual_trades or {}

    def _effective_params(self, code: str, holding: dict) -> dict:
        p = dict(self.params or {})
        stock_params = p.get("STOCK_PARAMS") if isinstance(p.get("STOCK_PARAMS"), dict) else {}
        if stock_params:
            p.update(stock_params.get(code, {}))
        if holding.get("type") == "etf" and isinstance(p.get("ETF_T0_PARAMS"), dict):
            p = {**p, **p.get("ETF_T0_PARAMS", {})}
        return p

    # ==================== 仓位上限 ====================

    def _current_price(self, holding: dict, index_ctx: dict, signal_price: float = 0.0) -> float:
        """估算当前价：优先用信号价，其次用前收×(1+日内涨跌幅)"""
        if signal_price > 0:
            return signal_price
        prev_close = float(holding.get("pre_close") or index_ctx.get("daily_prev_close") or 0)
        day_ret = float(index_ctx.get("daily_day_ret") or 0)
        if prev_close > 0:
            return prev_close * (1 + day_ret)
        return 0.0

    def _check_position_limit(self, code: str, holding: dict, index_ctx: dict,
                               current_price: float = 0.0, total_equity: float = 0.0) -> int:
        """单股仓位上限检查。返回超过上限的股数（需卖出部分）。

        total_equity: 账户总资产（由上层 Risk Manager / SESSION_CONTEXT 传入）。
                     为 0 时跳过仓位上限检查（不阻断交易）。
        """
        price = self._current_price(holding, index_ctx, current_price)
        max_pct = float(self.params.get("max_single_position_pct", 0.30) or 0.30)
        if max_pct >= 1.0 or total_equity <= 0:
            return 0

        cost = float(holding.get("cost") or 0)
        qty = int(holding.get("qty") or 0)
        if qty <= 0 or price <= 0:
            return 0
        if cost <= 0:
            # fix P0-9(B7): cost 缺失导致上限检查静默失效，显式告警标注数据缺失
            log.warning(f"⚠️ {code} 持仓 cost 缺失/非正(cost={cost})，单股上限检查跳过（数据缺失）")
            return 0
        market_value = price * qty
        # fix P0-9(B4): 按底层代码（去掉 _B 后缀）合并 A/B 双账户持仓市值后再判定
        base_code = str(code).split("_")[0]
        for _c, _h in (_ALL_HOLDINGS_REF or {}).items():
            if _c == code or str(_c).split("_")[0] != base_code:
                continue
            _q = int(_h.get("qty") or 0)
            if _q <= 0:
                continue
            _p = self._current_price(_h, index_ctx, current_price)
            if _p <= 0:
                continue
            market_value += _p * _q
        max_allowed = total_equity * max_pct
        excess_value = market_value - max_allowed
        if excess_value <= 0:
            return 0
        excess_qty = int(excess_value / price) if price > 0 else 0
        min_unit = 100
        # fix P0-9(B5): 向上取整到整手；若放大后超过可卖净量则取净量向下整手
        excess_qty = ((excess_qty + min_unit - 1) // min_unit) * min_unit
        if excess_qty > qty:
            excess_qty = (qty // min_unit) * min_unit
        return min(excess_qty, qty)

    def calc_sell_qty(self, code: str, holding: dict, regime,
                      sig_score: float, threshold: float,
                      used_sells: int = 0, index_ctx: dict = None,
                      current_price: float = 0.0, total_equity: float = 0.0) -> int:
        """
        计算卖出股数

        参数:
            code: 股票代码
            holding: 持仓信息（含 t_qty, qty, type, cost）
            regime: 市场状态（MarketRegime）
            sig_score: 信号评分
            threshold: 触发阈值
            used_sells: 本日已卖出次数
            index_ctx: 大盘上下文（含 daily_gate / index_circuit_state 等）
            current_price: 当前成交价（用于止损计算）

        返回: 建议卖出股数（整数，100的倍数）
        """
        # fix P0-9(B3): 纯底仓口径——严格 t_qty，不回退 qty；t_qty=0 不卖（与 main.py 动态份数口径一致）
        total_t = int(holding.get("t_qty", 0) or 0)
        if total_t <= 0:
            return 0

        net_qty = self._virtual_net_qty(code, holding)
        if net_qty <= 0:
            return 0

        index_ctx = index_ctx or {}

        # 仓位上限检查（超出上限的部分强制卖出）
        excess_pos_limit = self._check_position_limit(code, holding, index_ctx, current_price, total_equity)

        is_etf = holding.get("type") == "etf"
        p = self._effective_params(code, holding)
        strength = sig_score - threshold
        index_state = str(index_ctx.get("index_circuit_state", "normal") or "normal")
        index_factor = float(index_ctx.get("index_pos_factor", 1.0) or 1.0)
        target_qty = max(0, int(total_t * index_factor))
        excess_qty = max(0, net_qty - target_qty, excess_pos_limit)

        # 场景1：大盘熔断/清仓/减仓 + 场景3：正常模式
        # （改写为赋值+最终取max，确保stop_loss_qty起保底作用）
        if index_state == "clear" or should_clear_all(regime):
            result_qty = net_qty
        elif index_state == "reduce" or should_reduce(regime):
            if excess_qty > 0:
                if is_etf:
                    min_unit = p.get("etf_min_trade_unit", 100)
                else:
                    min_unit = p.get("stock_min_trade_unit", 100)
                # fix P0-9(B6): 向上取整到 min_unit；放大后超过可卖净量则取净量向下整手
                result_qty = ((excess_qty + min_unit - 1) // min_unit) * min_unit
                if result_qty > net_qty:
                    result_qty = (net_qty // min_unit) * min_unit
            elif is_etf:
                min_unit = p.get("etf_min_trade_unit", 100)
                result_qty = max(min_unit, (net_qty * 0.5 // min_unit) * min_unit)
                if result_qty > net_qty:  # fix P0-9(B6): 减半卖出 min_unit 放大后不超过可卖净量
                    result_qty = (net_qty // min_unit) * min_unit
            else:
                min_unit = p.get("stock_min_trade_unit", 100)
                result_qty = max(min_unit, (net_qty * 0.5 // min_unit) * min_unit)
                if result_qty > net_qty:  # fix P0-9(B6): 同上
                    result_qty = (net_qty // min_unit) * min_unit
        elif index_state == "defensive":
            if excess_qty > 0:
                if is_etf:
                    min_unit = p.get("etf_min_trade_unit", 100)
                else:
                    min_unit = p.get("stock_min_trade_unit", 100)
                # fix P0-9(B6): 向上取整到 min_unit；放大后超过可卖净量则取净量向下整手
                result_qty = ((excess_qty + min_unit - 1) // min_unit) * min_unit
                if result_qty > net_qty:
                    result_qty = (net_qty // min_unit) * min_unit
            else:
                # fall through to normal sizing with tighter factor
                if index_factor < 1.0 and excess_qty <= 0:
                    qty_cap = max(0, int(net_qty * index_factor))
                    if qty_cap <= 0:
                        result_qty = 0
                    else:
                        net_qty = min(net_qty, qty_cap)
                        result_qty = self._calc_etf_sell_qty(p, net_qty, strength, used_sells) if is_etf else self._calc_stock_sell_qty(p, net_qty, strength, used_sells)
                else:
                    result_qty = self._calc_etf_sell_qty(p, net_qty, strength, used_sells) if is_etf else self._calc_stock_sell_qty(p, net_qty, strength, used_sells)
        else:
            # 场景3：正常模式 → 分批卖出
            if index_factor < 1.0 and excess_qty <= 0:
                qty_cap = max(0, int(net_qty * index_factor))
                if qty_cap <= 0:
                    result_qty = 0
                else:
                    net_qty = min(net_qty, qty_cap)
                    result_qty = self._calc_etf_sell_qty(p, net_qty, strength, used_sells) if is_etf else self._calc_stock_sell_qty(p, net_qty, strength, used_sells)
            else:
                result_qty = self._calc_etf_sell_qty(p, net_qty, strength, used_sells) if is_etf else self._calc_stock_sell_qty(p, net_qty, strength, used_sells)

        available_qty = self._available_sell_qty(holding)
        # V1.2.1 (2026-08-11 用户拍板): 底仓地板默认取消——"手动跟单场景，做T不用考虑底仓问题"；
        # sell_floor_enabled=True 时恢复 V1.30 钳制（回测对照开关；sell_floor_ratio 参数保留）
        try:
            if p.get("sell_floor_enabled", False):
                _base_qty = int(holding.get("base") or holding.get("t_qty") or holding.get("qty") or 0)
                _floor_qty = int(_base_qty * float(p.get("sell_floor_ratio", 0.5)))
                _sell_cap = max(0, net_qty - _floor_qty)
            else:
                _sell_cap = net_qty
        except Exception:
            _sell_cap = net_qty
        return max(0, min(result_qty, net_qty, available_qty, _sell_cap))

    # ==================== 核心：买入份数计算 ====================

    def calc_buy_qty(self, code: str, holding: dict, regime,
                     sig_score: float, threshold: float, index_ctx: dict = None,
                     current_price: float = 0.0, total_equity: float = 0.0) -> int:
        """
        计算买入股数

        核心逻辑：
        1. 有未接回量 → 优先接回（高抛低吸组合拳）
        2. 无未接回量 → 按总T仓比例首次买入/加仓
        3. 重压模式下 → 谨慎接回（只接回30%）

        返回: 建议买入股数（整数，100的倍数）
        """
        # fix P0-9(B3): 纯底仓口径——严格 t_qty，不回退 qty；t_qty=0 不买
        total_t = int(holding.get("t_qty", 0) or 0)
        if total_t <= 0:
            return 0

        net_qty = self._virtual_net_qty(code, holding)
        is_etf = holding.get("type") == "etf"
        p = self._effective_params(code, holding)
        # V1.2.1 (2026-08-11 用户拍板): 满仓不再早退（原 max_buyable<=0 return 0 = 冻结链来源②）——
        # 继续走 unrebuilt/first_add 计算，末端保底一手；是否跟单由人决定
        # V1.2.2 (2026-08-11 用户拍板): 满仓买入建议开关化——V1.2.1 放开后实盘暴露副作用
        # （08-11 600481 满仓买×3 与卖单互抵），拍板"满仓保底买先不用保留"。
        # allow_full_position_buy=False（默认）→ 恢复早退 return 0；True → 走 V1.2.1 保底一手。
        max_buyable = max(0, total_t - net_qty)
        if max_buyable <= 0 and not p.get("allow_full_position_buy", False):
            return 0

        index_ctx = index_ctx or {}

        # V1.27: 仓位上限 → 如果已超上限，禁止继续买入
        excess_pos = self._check_position_limit(code, holding, index_ctx, current_price, total_equity)
        if excess_pos > 0:
            return 0

        # W33 B2 (2026-08-13): strength 三档失效——V2 纯两点后 sig_score 恒=100、阈值 36 → strength 恒≥10，
        # 永远走"强"档。简化为单档固定比例：个股 接回×0.60 / 首加×0.20；ETF 接回/首加×0.25。
        # （strentth 计算删除；sig_score/threshold 参数保留签名兼容，harness/main 位置传参）
        index_state = str(index_ctx.get("index_circuit_state", "normal") or "normal")
        index_factor = float(index_ctx.get("index_pos_factor", 1.0) or 1.0)

        # 计算已卖出未接回量
        unrebuilt = self._calc_unrebuilt(code)

        # 场景1：熔断/观望/减仓 → 不买
        if index_state in {"clear", "reduce", "stand_aside"} or should_clear_all(regime):
            return 0

        # 场景2：防守模式 → 只允许低风险接回，且受大盘目标仓位限制
        if index_factor <= 0:
            return 0
        target_cap = max(0, int(total_t * index_factor))
        max_buyable = min(max_buyable, max(0, target_cap - net_qty))
        # V1.2.1: 此处不再因 max_buyable<=0 早退（同为满仓冻结链一环）；
        # 大盘目标仓位仍作为数量上限参与下方 min(qty, max_buyable) 钳制（风控保留，冻结取消）
        # V1.2.2: 大盘目标仓位钳到 0 视同"算不出可买量"——开关关（默认）同样早退；开关开走 V1.2.1 保底一手
        if max_buyable <= 0 and not p.get("allow_full_position_buy", False):
            return 0

        # 建仓/加仓时机判定（timing_gate，regime条件化：多头追强/空头抄底/震荡降频）
        # 2026-08-15 接入加仓侧：NO-GO 时阻断加仓买入（降频），接回是否阻断由
        # ENTRY_TIMING_PARAMS.add_block_rebuild 控制；时机模块故障不阻断交易
        try:
            from timing_gate import timing_verdict as _timing_verdict
            from config import ENTRY_TIMING_PARAMS as _ETP
            if _ETP.get("apply_to_add", True) and _ETP.get("enabled", True):
                if not _timing_verdict(code, None).get("go", False):
                    if unrebuilt > 0 and not _ETP.get("add_block_rebuild", True):
                        pass  # 接回放行
                    else:
                        return 0
        except Exception:
            pass

        if unrebuilt > 0:
            pct = p.get("etf_buy_qty_pct", 0.25) if is_etf else p.get("stock_rebuild_pct", 0.60)
            qty = int(unrebuilt * pct)
        else:
            pct = p.get("etf_buy_qty_pct", 0.25) if is_etf else p.get("stock_first_add_pct", 0.20)
            qty = int(total_t * pct)

        # 确保不超过剩余可买额度
        qty = min(qty, max_buyable)

        # V1.2.1 (2026-08-11 用户拍板): 计算量<=0 时保底一手（原 fix P0-9(B2) 返回 0 = 冻结链；
        # 手动跟单场景信号达标即给出可执行建议数量，个股/ETF 统一）
        min_unit = p.get("etf_min_trade_unit", 100) if is_etf else p.get("stock_min_trade_unit", 100)
        if qty <= 0:
            qty = min_unit
        qty = max(min_unit, (qty // min_unit) * min_unit)

        return max(0, qty)

    # ==================== 内部辅助方法 ====================

    def _calc_stock_sell_qty(self, p: dict, net_qty: int, strength: float, used_sells: int) -> int:
        """个股正常模式下的分批卖出"""
        min_unit = p.get("stock_min_trade_unit", 100)

        if used_sells == 0:
            # 首次卖出
            if strength >= 10:
                pct = p.get("stock_qty_strong_pct", 0.40)
            elif strength >= 5:
                pct = p.get("stock_qty_base_pct", 0.30)
            else:
                pct = p.get("stock_qty_weak_pct", 0.20)
        elif used_sells == 1:
            # 二次卖出：卖出剩余可卖的更大比例
            if strength >= 10:
                pct = 0.60
            elif strength >= 5:
                pct = 0.40
            else:
                pct = 0.20
        else:
            # 三次及以上
            if strength >= 10:
                pct = 1.0  # 清仓
            elif strength >= 5:
                pct = 0.50
            else:
                return 0

        qty = int(net_qty * pct)
        # V1.2.1 (2026-08-11 用户拍板): int(net×pct) 不足一手时保底 100 股（原: 返回 0 = 冻结链来源①）；
        # pct>=1.0 清仓逻辑不变；used_sells>=2 弱信号 return 0（轮次停止，风控）保留
        if pct >= 1.0:
            return net_qty
        if qty < min_unit:
            return min(min_unit, net_qty) if net_qty > 0 else 0
        return max(min_unit, (qty // min_unit) * min_unit)

    def _calc_etf_sell_qty(self, p: dict, net_qty: int, strength: float, used_sells: int) -> int:
        """ETF正常模式下的分批卖出（保持原有逻辑）"""
        min_unit = p.get("etf_min_trade_unit", 100)
        max_cycles = p.get("max_t_cycles_per_stock", 8)
        used_sells = min(used_sells, max_cycles - 1)
        remaining = max(1, p.get("max_sell_times_per_stock", 5) - used_sells)

        if strength >= 10:
            strength_pct = p.get("etf_qty_strong_pct", 0.25)
        elif strength >= 5:
            strength_pct = p.get("etf_qty_base_pct", 0.15)
        else:
            strength_pct = p.get("etf_qty_weak_pct", 0.08)

        base_qty = int(net_qty * strength_pct)
        remaining_factor = min(2.0, 1.0 + (3 - remaining) * 0.3) if remaining <= 3 else 1.0
        qty = int(base_qty * remaining_factor)
        qty = max(min_unit, (qty // min_unit) * min_unit)
        return min(qty, net_qty)

    def _virtual_net_qty(self, code: str, holding: dict) -> int:
        """计算当前虚拟净持仓（可卖量）"""
        # fix P0-9(B3): 纯底仓口径——严格 t_qty，不再回退 qty
        base_qty = int(holding.get("t_qty") or 0)
        if code not in self.virtual_trades:
            return base_qty
        buys = self.virtual_trades[code].get("BUY_LOW", [])
        sells = self.virtual_trades[code].get("SELL_HIGH", [])
        return max(0, base_qty + sum(t.get("qty", 0) for t in buys) - sum(t.get("qty", 0) for t in sells))

    def _available_sell_qty(self, holding: dict) -> int:
        available = holding.get("available")
        if available is not None:
            try:
                return max(0, int(available or 0))
            except Exception:
                return 0
        return max(0, int(holding.get("qty") or holding.get("t_qty") or 0))

    def _calc_unrebuilt(self, code: str) -> int:
        """计算已卖出但未接回的量"""
        if code not in self.virtual_trades:
            return 0
        sells = self.virtual_trades[code].get("SELL_HIGH", [])
        buys = self.virtual_trades[code].get("BUY_LOW", [])
        total_sold = sum(t.get("qty", 0) for t in sells)
        total_bought = sum(t.get("qty", 0) for t in buys)
        return max(0, total_sold - total_bought)


# ==================== 便捷函数（供共享命名空间调用） ====================

_default_sizer = None

def get_sizer(params: dict = None, virtual_trades: dict = None) -> PositionSizer:
    global _default_sizer
    if _default_sizer is None or params is not None:
        _default_sizer = PositionSizer(params=params, virtual_trades=virtual_trades)
    return _default_sizer


def calc_sell_qty(code: str, holding: dict, regime, sig_score: float, threshold: float,
                  used_sells: int = 0, params: dict = None, virtual_trades: dict = None, index_ctx: dict = None,
                  current_price: float = 0.0, total_equity: float = 0.0) -> int:
    """便捷函数：计算卖出股数"""
    return get_sizer(params, virtual_trades).calc_sell_qty(code, holding, regime, sig_score, threshold, used_sells, index_ctx=index_ctx, current_price=current_price, total_equity=total_equity)


def calc_buy_qty(code: str, holding: dict, regime, sig_score: float, threshold: float,
                 params: dict = None, virtual_trades: dict = None, index_ctx: dict = None,
                 current_price: float = 0.0, total_equity: float = 0.0) -> int:
    """便捷函数：计算买入股数"""
    return get_sizer(params, virtual_trades).calc_buy_qty(code, holding, regime, sig_score, threshold, index_ctx=index_ctx, current_price=current_price, total_equity=total_equity)
