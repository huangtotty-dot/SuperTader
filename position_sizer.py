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
    """动态仓位管理器"""

    def __init__(self, params: dict = None, virtual_trades: dict = None):
        """
        参数:
            params: 全局参数（从 config.py 的 PARAMS 传入）
            virtual_trades: 虚拟交易记录（从 signal_engine 的 VIRTUAL_TRADES 传入）
        """
        self.params = params or {}
        self.virtual_trades = virtual_trades or {}

    # ==================== 核心：卖出份数计算 ====================

    def calc_sell_qty(self, code: str, holding: dict, regime, 
                      sig_score: float, threshold: float,
                      used_sells: int = 0) -> int:
        """
        计算卖出股数

        参数:
            code: 股票代码
            holding: 持仓信息（含 t_qty, qty, type）
            regime: 市场状态（MarketRegime）
            sig_score: 信号评分
            threshold: 触发阈值
            used_sells: 本日已卖出次数

        返回: 建议卖出股数（整数，100的倍数）
        """
        total_t = int(holding.get("t_qty", 0) or holding.get("qty", 0) or 0)
        if total_t <= 0:
            return 0

        net_qty = self._virtual_net_qty(code, holding)
        if net_qty <= 0:
            return 0

        is_etf = holding.get("type") == "etf"
        p = self.params if not is_etf else {**self.params, **self.params.get("ETF_T0_PARAMS", {})}
        strength = sig_score - threshold

        # 场景1：重压/出货 → 全清或大减仓
        if should_clear_all(regime):
            if is_etf:
                # ETF 重压下：卖80%（保留T+0灵活性）
                return max(p.get("etf_min_trade_unit", 100), 
                          (net_qty * 0.8 // p.get("etf_min_trade_unit", 100)) * p.get("etf_min_trade_unit", 100))
            else:
                # 个股重压下：全清
                return net_qty

        # 场景2：早盘冲高回落 → 减仓50%
        if should_reduce(regime):
            if is_etf:
                return max(p.get("etf_min_trade_unit", 100),
                          (net_qty * 0.5 // p.get("etf_min_trade_unit", 100)) * p.get("etf_min_trade_unit", 100))
            else:
                return max(p.get("stock_min_trade_unit", 100),
                          (net_qty * 0.5 // p.get("stock_min_trade_unit", 100)) * p.get("stock_min_trade_unit", 100))

        # 场景3：正常模式 → 分批卖出
        if is_etf:
            return self._calc_etf_sell_qty(p, net_qty, strength, used_sells)
        else:
            return self._calc_stock_sell_qty(p, net_qty, strength, used_sells)

    # ==================== 核心：买入份数计算 ====================

    def calc_buy_qty(self, code: str, holding: dict, regime,
                     sig_score: float, threshold: float) -> int:
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

        is_etf = holding.get("type") == "etf"
        p = self.params if not is_etf else {**self.params, **self.params.get("ETF_T0_PARAMS", {})}
        strength = sig_score - threshold

        # 计算已卖出未接回量
        unrebuilt = self._calc_unrebuilt(code)

        # 场景1：重压模式 → 谨慎接回
        if should_clear_all(regime):
            if unrebuilt > 0:
                pct = p.get("stock_rebuild_tight_pct", 0.30) if not is_etf else p.get("etf_rebuild_tight_pct", 0.30)
                qty = int(unrebuilt * pct)
            else:
                # 重压下不主动加仓
                return 0
        else:
            # 场景2：正常模式 → 优先接回
            if unrebuilt > 0:
                if is_etf:
                    pct = p.get("etf_qty_strong_pct", 0.25) if strength >= 10 else \
                          (p.get("etf_qty_base_pct", 0.15) if strength >= 5 else p.get("etf_qty_weak_pct", 0.08))
                else:
                    pct = p.get("stock_rebuild_strong_pct", 0.80) if strength >= 10 else \
                          (p.get("stock_rebuild_base_pct", 0.50) if strength >= 5 else p.get("stock_rebuild_weak_pct", 0.30))
                qty = int(unrebuilt * pct)
            else:
                # 场景3：首次买入/加仓（从未卖出过）
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
                  used_sells: int = 0, params: dict = None, virtual_trades: dict = None) -> int:
    """便捷函数：计算卖出股数"""
    return get_sizer(params, virtual_trades).calc_sell_qty(code, holding, regime, sig_score, threshold, used_sells)


def calc_buy_qty(code: str, holding: dict, regime, sig_score: float, threshold: float,
                 params: dict = None, virtual_trades: dict = None) -> int:
    """便捷函数：计算买入股数"""
    return get_sizer(params, virtual_trades).calc_buy_qty(code, holding, regime, sig_score, threshold)


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
