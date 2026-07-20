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

from typing import Dict, Any, Optional
from market_regime import MarketRegime, should_clear_all, should_reduce


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

    # ==================== V1.27: 日线止损 + 仓位上限 ====================

    def _current_price(self, holding: dict, index_ctx: dict, signal_price: float = 0.0) -> float:
        """估算当前价：优先用信号价，其次用前收×(1+日内涨跌幅)"""
        if signal_price > 0:
            return signal_price
        prev_close = float(holding.get("pre_close") or index_ctx.get("daily_prev_close") or 0)
        day_ret = float(index_ctx.get("daily_day_ret") or 0)
        if prev_close > 0:
            return prev_close * (1 + day_ret)
        return 0.0

    def _loss_pct(self, current_price: float, holding: dict) -> float:
        """累计亏损率（基于持仓成本）"""
        cost = float(holding.get("cost") or 0)
        if cost <= 0 or current_price <= 0:
            return 0.0
        return (current_price - cost) / cost

    def _check_daily_stop_loss(self, code: str, holding: dict, index_ctx: dict,
                                current_price: float = 0.0) -> int:
        """日线止损检查。返回 >0 时强制卖出对应数量，覆盖正常仓位计算。"""
        price = self._current_price(holding, index_ctx, current_price)
        loss_pct = self._loss_pct(price, holding)
        net_qty = self._virtual_net_qty(code, holding)
        if net_qty <= 0:
            return 0

        hard_breakdown = bool(index_ctx.get("daily_hard_breakdown", False))
        breakdown_risk = bool(index_ctx.get("daily_breakdown_risk", False))
        min_unit = 100

        # P0: 累计亏损 > 25% → 强制清仓（不做T了，全部卖出止损）
        if loss_pct <= -0.25:
            return net_qty

        # P1: 日线硬破位（跌破MA60 3.5%）→ 强制卖出 50%
        if hard_breakdown:
            half = max(min_unit, (net_qty * 0.5 // min_unit) * min_unit)
            return half

        # P2: 日线破位风险 + 累计亏损 > 15% → 强制卖出 30%
        if breakdown_risk and loss_pct <= -0.15:
            pct_sell = 0.30
            qty = max(min_unit, (net_qty * pct_sell // min_unit) * min_unit)
            return qty

        return 0

    def _check_position_limit(self, code: str, holding: dict, index_ctx: dict,
                               current_price: float = 0.0) -> int:
        """单股仓位上限检查。返回超过上限的股数（需卖出部分）。"""
        price = self._current_price(holding, index_ctx, current_price)
        max_pct = float(self.params.get("max_single_position_pct", 0.30) or 0.30)
        if max_pct >= 1.0:
            return 0

        # 估算总资金 = 本股（cost×qty）÷ 本股占比（保守用 1/max_pct）
        cost = float(holding.get("cost") or 0)
        qty = int(holding.get("qty") or 0)
        if cost <= 0 or qty <= 0 or price <= 0:
            return 0
        market_value = price * qty
        estimated_total = market_value / max_pct
        if estimated_total <= 0:
            return 0
        max_value = estimated_total * max_pct
        excess_value = market_value - max_value
        if excess_value <= 0:
            return 0
        excess_qty = int(excess_value / price) if price > 0 else 0
        min_unit = 100
        excess_qty = max(min_unit, (excess_qty // min_unit) * min_unit)
        return min(excess_qty, qty)

    def calc_sell_qty(self, code: str, holding: dict, regime,
                      sig_score: float, threshold: float,
                      used_sells: int = 0, index_ctx: dict = None,
                      current_price: float = 0.0) -> int:
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
        total_t = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        if total_t <= 0:
            return 0

        net_qty = self._virtual_net_qty(code, holding)
        if net_qty <= 0:
            return 0

        index_ctx = index_ctx or {}

        # V1.27: 日线止损检查（得到最低止损量，后续与正常逻辑取最大值）
        stop_loss_qty = self._check_daily_stop_loss(code, holding, index_ctx, current_price)

        # V1.27: 仓位上限检查（超出上限的部分强制卖出）
        excess_pos_limit = self._check_position_limit(code, holding, index_ctx, current_price)

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
                result_qty = max(min_unit, (excess_qty // min_unit) * min_unit)
            elif is_etf:
                result_qty = max(p.get("etf_min_trade_unit", 100), (net_qty * 0.5 // p.get("etf_min_trade_unit", 100)) * p.get("etf_min_trade_unit", 100))
            else:
                result_qty = max(p.get("stock_min_trade_unit", 100), (net_qty * 0.5 // p.get("stock_min_trade_unit", 100)) * p.get("stock_min_trade_unit", 100))
        elif index_state == "defensive":
            if excess_qty > 0:
                if is_etf:
                    min_unit = p.get("etf_min_trade_unit", 100)
                else:
                    min_unit = p.get("stock_min_trade_unit", 100)
                result_qty = max(min_unit, (excess_qty // min_unit) * min_unit)
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

        # V1.27: 日线止损保底 — 取正常逻辑和止损量的较大值
        if stop_loss_qty > 0:
            result_qty = max(result_qty, stop_loss_qty)
        return max(0, min(result_qty, net_qty))

    # ==================== 核心：买入份数计算 ====================

    def calc_buy_qty(self, code: str, holding: dict, regime,
                     sig_score: float, threshold: float, index_ctx: dict = None,
                     current_price: float = 0.0) -> int:
        """
        计算买入股数

        核心逻辑：
        1. 有未接回量 → 优先接回（高抛低吸组合拳）
        2. 无未接回量 → 按总T仓比例首次买入/加仓
        3. 重压模式下 → 谨慎接回（只接回30%）

        返回: 建议买入股数（整数，100的倍数）
        """
        total_t = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        if total_t <= 0:
            return 0

        net_qty = self._virtual_net_qty(code, holding)
        max_buyable = max(0, total_t - net_qty)
        if max_buyable <= 0:
            return 0

        index_ctx = index_ctx or {}

        # V1.27: 仓位上限 → 如果已超上限，禁止继续买入
        excess_pos = self._check_position_limit(code, holding, index_ctx, current_price)
        if excess_pos > 0:
            return 0

        is_etf = holding.get("type") == "etf"
        p = self._effective_params(code, holding)
        strength = sig_score - threshold
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
        if max_buyable <= 0:
            return 0

        if unrebuilt > 0:
            if is_etf:
                pct = p.get("etf_qty_strong_pct", 0.25) if strength >= 10 else \
                      (p.get("etf_qty_base_pct", 0.15) if strength >= 5 else p.get("etf_qty_weak_pct", 0.08))
            else:
                pct = p.get("stock_rebuild_strong_pct", 0.80) if strength >= 10 else \
                      (p.get("stock_rebuild_base_pct", 0.50) if strength >= 5 else p.get("stock_rebuild_weak_pct", 0.30))
            qty = int(unrebuilt * pct)
        else:
            if is_etf:
                pct = p.get("etf_qty_strong_pct", 0.25) if strength >= 10 else \
                      (p.get("etf_qty_base_pct", 0.15) if strength >= 5 else p.get("etf_qty_weak_pct", 0.08))
            else:
                pct = p.get("stock_first_add_strong_pct", 0.30) if strength >= 10 else \
                      (p.get("stock_first_add_pct", 0.20) if strength >= 5 else p.get("stock_first_add_weak_pct", 0.10))
            qty = int(total_t * pct)

        # 确保不超过剩余可买额度
        qty = min(qty, max_buyable)

        # 最小交易单位
        min_unit = p.get("etf_min_trade_unit", 100) if is_etf else p.get("stock_min_trade_unit", 100)
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
        return max(min_unit, (qty // min_unit) * min_unit) if qty >= min_unit else (net_qty if pct >= 1.0 else 0)

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
        base_qty = int(holding.get("t_qty") or holding.get("qty") or 0)
        if code not in self.virtual_trades:
            return base_qty
        buys = self.virtual_trades[code].get("BUY_LOW", [])
        sells = self.virtual_trades[code].get("SELL_HIGH", [])
        return max(0, base_qty + sum(t.get("qty", 0) for t in buys) - sum(t.get("qty", 0) for t in sells))

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
                  current_price: float = 0.0) -> int:
    """便捷函数：计算卖出股数"""
    return get_sizer(params, virtual_trades).calc_sell_qty(code, holding, regime, sig_score, threshold, used_sells, index_ctx=index_ctx, current_price=current_price)


def calc_buy_qty(code: str, holding: dict, regime, sig_score: float, threshold: float,
                 params: dict = None, virtual_trades: dict = None, index_ctx: dict = None,
                 current_price: float = 0.0) -> int:
    """便捷函数：计算买入股数"""
    return get_sizer(params, virtual_trades).calc_buy_qty(code, holding, regime, sig_score, threshold, index_ctx=index_ctx, current_price=current_price)


def get_trade_summary(code: str, virtual_trades: dict = None) -> dict:
    """获取交易摘要（用于飞书通知）"""
    vt = virtual_trades or {}
    sells = vt.get(code, {}).get("SELL_HIGH", [])
    buys = vt.get(code, {}).get("BUY_LOW", [])
    total_sold = sum(t.get("qty", 0) for t in sells)
    total_bought = sum(t.get("qty", 0) for t in buys)
    unrebuilt = max(0, total_sold - total_bought)
    return {
        "total_sold": total_sold,
        "total_bought": total_bought,
        "unrebuilt": unrebuilt,
        "sell_count": len(sells),
        "buy_count": len(buys),
    }
