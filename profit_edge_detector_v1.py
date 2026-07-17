# -*- coding: utf-8 -*-
"""
盈利边缘检测器 - t_trader v1.11 P0 优先级
实时检测持仓盈利/亏损状态，在盈利边缘触发紧急卖出

核心逻辑：
1. 持续监控每笔持仓的实时收益率
2. 检测"盈利边缘"状态（接近零点的小幅盈利）
3. 立即触发卖出信号（P0最高优先级）
4. 避免盈利转亏损的风险
"""
from datetime import datetime
from typing import Dict, Tuple, Optional, List
from enum import Enum
import logging

logger = logging.getLogger(__name__)

class ProfitState(Enum):
    """持仓盈利状态"""
    DEEP_LOSS = "deep_loss"           # 深度亏损 < -3%
    SIGNIFICANT_LOSS = "sig_loss"     # 显著亏损 -3% ~ -1%
    SLIGHT_LOSS = "slight_loss"       # 轻微亏损 -1% ~ -0.5%
    EDGE_LOSS = "edge_loss"           # 亏损边缘 -0.5% ~ 0%
    EDGE_PROFIT = "edge_profit"       # 盈利边缘 0% ~ +0.5%
    SLIGHT_PROFIT = "slight_profit"   # 轻微盈利 +0.5% ~ +1%
    SIGNIFICANT_PROFIT = "sig_profit" # 显著盈利 +1% ~ +3%
    DEEP_PROFIT = "deep_profit"       # 深度盈利 > +3%

class ProfitEdgeDetector:
    """
    盈利边缘检测器 - 实时监控持仓盈利/亏损

    功能：
    1. 计算每笔持仓的实时收益率
    2. 检测盈利边缘状态（最危险的状态）
    3. 生成P0优先级的紧急卖出信号
    4. 统计盈利/亏损分布
    5. 生成风险报告

    配置参数：
    - edge_threshold: 盈利边缘的定义范围 (默认0.5%)
    - urgent_sell_profit_trigger: 盈利触发卖出的阈值 (默认0.3%)
    - urgent_sell_loss_limit: 亏损止损线 (默认-2%)
    """

    def __init__(self, edge_threshold: float = 0.005,
                 urgent_sell_profit_trigger: float = 0.003,
                 urgent_sell_loss_limit: float = -0.02):
        """
        初始化盈利边缘检测器

        Args:
            edge_threshold: 盈利边缘范围(默认0.5%)
            urgent_sell_profit_trigger: 盈利卖出阈值(默认0.3%)
            urgent_sell_loss_limit: 亏损止损限(-2%)
        """
        self.edge_threshold = edge_threshold
        self.urgent_sell_profit_trigger = urgent_sell_profit_trigger
        self.urgent_sell_loss_limit = urgent_sell_loss_limit

        # 实时监控数据
        self.holdings: Dict[str, Dict] = {}  # {code: {entry_price, quantity, entry_time, ...}}
        self.edge_detection_history = []  # 历史边缘检测记录
        self.urgent_signals = []  # 紧急卖出信号队列

        # 统计数据
        self.stats = {
            "total_holdings": 0,
            "profitable_count": 0,
            "loss_count": 0,
            "edge_profit_count": 0,
            "edge_loss_count": 0,
            "total_profit": 0.0,
            "total_loss": 0.0,
            "avg_profit_rate": 0.0,
        }

    def add_holding(self, code: str, entry_price: float, quantity: int,
                   current_price: float = None) -> bool:
        """
        添加持仓到监控列表

        Args:
            code: 股票代码
            entry_price: 建仓价格
            quantity: 持仓数量
            current_price: 当前价格(可选，用于初始化)

        Returns:
            是否添加成功
        """
        if code in self.holdings:
            logger.warning(f"⚠️  {code} 已存在持仓记录，覆盖旧数据")

        self.holdings[code] = {
            "entry_price": entry_price,
            "quantity": quantity,
            "current_price": current_price or entry_price,
            "entry_time": datetime.now(),
            "last_check_time": datetime.now(),
            "profit_rate": 0.0,
            "profit_state": ProfitState.EDGE_PROFIT,
            "is_edge_detected": False,
            "edge_detection_time": None,
        }

        logger.info(f"✅ 添加持仓监控: {code} {quantity}股 @ {entry_price:.2f}")
        return True

    def update_price(self, code: str, current_price: float) -> Optional[Dict]:
        """
        更新持仓的当前价格并计算收益率

        Args:
            code: 股票代码
            current_price: 最新价格

        Returns:
            更新后的持仓信息字典，或None（持仓不存在）
        """
        if code not in self.holdings:
            logger.warning(f"❌ {code} 不在监控列表中")
            return None

        holding = self.holdings[code]
        entry_price = holding["entry_price"]

        # 计算收益率
        profit_rate = (current_price - entry_price) / entry_price

        holding["current_price"] = current_price
        holding["profit_rate"] = profit_rate
        holding["last_check_time"] = datetime.now()

        # 更新盈利状态
        old_state = holding["profit_state"]
        holding["profit_state"] = self._classify_profit_state(profit_rate)

        # 状态转换时记录
        if old_state != holding["profit_state"]:
            logger.debug(
                f"🔄 {code} 状态转换: {old_state.value} → {holding['profit_state'].value} "
                f"(收益率: {profit_rate*100:+.2f}%)"
            )

        return holding

    def _classify_profit_state(self, profit_rate: float) -> ProfitState:
        """
        根据收益率分类盈利状态

        Args:
            profit_rate: 收益率 (0.05 = 5%)

        Returns:
            ProfitState 枚举值
        """
        if profit_rate < -0.03:
            return ProfitState.DEEP_LOSS
        elif profit_rate < -0.01:
            return ProfitState.SIGNIFICANT_LOSS
        elif profit_rate < -0.005:
            return ProfitState.SLIGHT_LOSS
        elif profit_rate < 0:
            return ProfitState.EDGE_LOSS
        elif profit_rate < self.edge_threshold:
            return ProfitState.EDGE_PROFIT
        elif profit_rate < 0.01:
            return ProfitState.SLIGHT_PROFIT
        elif profit_rate < 0.03:
            return ProfitState.SIGNIFICANT_PROFIT
        else:
            return ProfitState.DEEP_PROFIT

    def detect_edge_cases(self) -> List[Tuple[str, Dict, int]]:
        """
        检测所有盈利/亏损边缘的持仓

        返回：
            [(code, holding_info, urgency_score), ...]
            urgency_score: 0-100，值越高越紧急
        """
        edge_cases = []

        for code, holding in self.holdings.items():
            profit_rate = holding["profit_rate"]
            state = holding["profit_state"]

            urgency_score = 0
            reason = ""

            # 盈利边缘 - 最高优先级 (90-100)
            if state == ProfitState.EDGE_PROFIT:
                # 距离0%越近越紧急
                distance_to_zero = abs(profit_rate)
                urgency_score = int(100 - distance_to_zero * 10000)  # 0.3% -> 97分
                reason = f"盈利边缘 ({profit_rate*100:+.2f}%)"

            # 亏损边缘 - 高优先级 (70-89)
            elif state == ProfitState.EDGE_LOSS:
                distance_to_zero = abs(profit_rate)
                urgency_score = int(80 - distance_to_zero * 10000)  # 接近0% -> 80分
                reason = f"亏损边缘 ({profit_rate*100:+.2f}%)"

            # 即将转亏的盈利 - 中等优先级 (50-69)
            elif state == ProfitState.SLIGHT_PROFIT and profit_rate < 0.01:
                urgency_score = 60
                reason = f"轻微盈利 ({profit_rate*100:+.2f}%)"

            # 即将转盈的亏损 - 低优先级 (20-49)
            elif state == ProfitState.SLIGHT_LOSS and profit_rate > -0.01:
                urgency_score = 30
                reason = f"轻微亏损 ({profit_rate*100:+.2f}%)"

            if urgency_score > 0:
                edge_cases.append((code, holding, urgency_score))
                logger.debug(f"🎯 检测到边缘情况: {code} {reason} (紧急度:{urgency_score})")

                # 记录到历史
                self.edge_detection_history.append({
                    "time": datetime.now().isoformat(),
                    "code": code,
                    "state": state.value,
                    "profit_rate": profit_rate,
                    "urgency_score": urgency_score,
                    "reason": reason
                })

        # 按紧急度排序(高优先级在前)
        edge_cases.sort(key=lambda x: -x[2])

        return edge_cases

    def generate_urgent_sell_signals(self) -> List[Tuple[str, int, str]]:
        """
        生成紧急卖出信号 (P0最高优先级)

        返回：
            [(code, signal_score, reason), ...]
            signal_score: 100-150，150最高优先级
        """
        urgent_signals = []

        for code, holding in self.holdings.items():
            profit_rate = holding["profit_rate"]
            state = holding["profit_state"]
            signal_score = 0
            reason = ""

            # 规则1: 盈利边缘且接近0% -> 立即卖出
            if state == ProfitState.EDGE_PROFIT:
                # 距离0%越近分数越高
                distance = abs(profit_rate)
                signal_score = int(150 - distance * 10000)  # 最高150分
                reason = f"🔴P0紧急 盈利边缘({profit_rate*100:+.2f}%) - 防止转亏"

            # 规则2: 亏损即将触及止损线 -> 立即卖出
            elif state == ProfitState.EDGE_LOSS and profit_rate < self.urgent_sell_loss_limit * 1.5:
                signal_score = 140
                reason = f"🔴P0紧急 接近止损线({profit_rate*100:+.2f}%) - 紧急止损"

            # 规则3: 已经达到盈利触发点 -> 卖出
            elif profit_rate > 0 and profit_rate <= self.urgent_sell_profit_trigger:
                signal_score = 130
                reason = f"🟠P0重要 盈利锁定({profit_rate*100:+.2f}%) - 及时止盈"

            if signal_score > 0:
                urgent_signals.append((code, signal_score, reason))
                logger.warning(f"⚠️  {reason}")

        # 按分数排序
        urgent_signals.sort(key=lambda x: -x[1])

        return urgent_signals

    def get_holding_status(self, code: str) -> Optional[Dict]:
        """获取单个持仓的详细状态"""
        if code not in self.holdings:
            return None

        holding = self.holdings[code]
        entry_price = holding["entry_price"]
        current_price = holding["current_price"]
        profit_rate = holding["profit_rate"]

        return {
            "code": code,
            "entry_price": entry_price,
            "current_price": current_price,
            "quantity": holding["quantity"],
            "profit_rate_pct": f"{profit_rate*100:+.2f}%",
            "profit_value": (current_price - entry_price) * holding["quantity"],
            "state": holding["profit_state"].value,
            "entry_time": holding["entry_time"].isoformat(),
            "last_update": holding["last_check_time"].isoformat(),
        }

    def get_portfolio_summary(self) -> Dict:
        """获取投资组合总体摘要"""
        if not self.holdings:
            return {
                "total_holdings": 0,
                "total_value": 0,
                "total_profit_loss": 0,
                "portfolio_return": "0.00%",
                "profitable_count": 0,
                "loss_count": 0,
            }

        profitable = sum(1 for h in self.holdings.values() if h["profit_rate"] > 0)
        loss = sum(1 for h in self.holdings.values() if h["profit_rate"] < 0)

        total_value = sum(
            h["current_price"] * h["quantity"]
            for h in self.holdings.values()
        )
        total_cost = sum(
            h["entry_price"] * h["quantity"]
            for h in self.holdings.values()
        )
        total_pnl = total_value - total_cost

        portfolio_return = (total_pnl / total_cost * 100) if total_cost > 0 else 0

        return {
            "total_holdings": len(self.holdings),
            "total_cost": total_cost,
            "total_value": total_value,
            "total_profit_loss": total_pnl,
            "portfolio_return": f"{portfolio_return:+.2f}%",
            "profitable_count": profitable,
            "loss_count": loss,
            "edge_profit_count": sum(
                1 for h in self.holdings.values()
                if h["profit_state"] == ProfitState.EDGE_PROFIT
            ),
            "edge_loss_count": sum(
                1 for h in self.holdings.values()
                if h["profit_state"] == ProfitState.EDGE_LOSS
            ),
        }

    def print_status(self):
        """打印详细状态报告"""
        print("\n" + "="*80)
        print("💰 盈利边缘检测状态报告")
        print("="*80)

        if not self.holdings:
            print("暂无持仓")
            print("="*80 + "\n")
            return

        # 投资组合摘要
        summary = self.get_portfolio_summary()
        print(f"\n【投资组合摘要】")
        print(f"  总持仓: {summary['total_holdings']}笔")
        print(f"  总成本: ¥{summary['total_cost']:.2f}")
        print(f"  总价值: ¥{summary['total_value']:.2f}")
        print(f"  盈亏: ¥{summary['total_profit_loss']:+.2f}")
        print(f"  收益率: {summary['portfolio_return']}")
        print(f"  盈利: {summary['profitable_count']}笔 | 亏损: {summary['loss_count']}笔")

        # 详细持仓
        print(f"\n【详细持仓】")
        for code, holding in sorted(self.holdings.items()):
            status = self.get_holding_status(code)
            state_emoji = self._get_state_emoji(holding["profit_state"])
            print(f"\n  {state_emoji} {code}")
            print(f"     成本: ¥{holding['entry_price']:.2f} × {holding['quantity']}股")
            print(f"     现价: ¥{holding['current_price']:.2f}")
            print(f"     收益: {status['profit_rate_pct']} (¥{status['profit_value']:+.2f})")
            print(f"     状态: {holding['profit_state'].value}")

        # 紧急信号
        urgent = self.generate_urgent_sell_signals()
        if urgent:
            print(f"\n【🔴 紧急卖出信号 (P0最高优先级)】")
            for code, score, reason in urgent:
                print(f"  [{score}分] {reason}")

        # 边缘情况
        edges = self.detect_edge_cases()
        if edges:
            print(f"\n【⚠️  边缘监控情况】")
            for code, holding, score in edges[:5]:  # 只显示前5个
                print(f"  [{score}分] {code}: {holding['profit_rate']*100:+.2f}%")

        print("="*80 + "\n")

    @staticmethod
    def _get_state_emoji(state: ProfitState) -> str:
        """获取状态对应的表情符号"""
        emoji_map = {
            ProfitState.DEEP_LOSS: "🔴",
            ProfitState.SIGNIFICANT_LOSS: "🟠",
            ProfitState.SLIGHT_LOSS: "🟡",
            ProfitState.EDGE_LOSS: "⚠️",
            ProfitState.EDGE_PROFIT: "⚠️",
            ProfitState.SLIGHT_PROFIT: "🟡",
            ProfitState.SIGNIFICANT_PROFIT: "🟢",
            ProfitState.DEEP_PROFIT: "🟢",
        }
        return emoji_map.get(state, "❓")

    def close_holding(self, code: str) -> Optional[Dict]:
        """平仓一个持仓"""
        if code not in self.holdings:
            return None

        holding = self.holdings.pop(code)
        logger.info(f"✅ 平仓: {code} (收益率: {holding['profit_rate']*100:+.2f}%)")
        return holding


if __name__ == "__main__":
    # 测试代码
    print("\n🔬 盈利边缘检测器测试")

    detector = ProfitEdgeDetector(
        edge_threshold=0.005,  # 0.5% 边缘
        urgent_sell_profit_trigger=0.003,  # 0.3% 盈利卖出
        urgent_sell_loss_limit=-0.02  # -2% 止损线
    )

    # 模拟持仓
    detector.add_holding("603667", 65.30, 500, 65.32)  # 盈利0.03%
    detector.add_holding("002407", 35.22, 200, 34.95)  # 亏损-0.76%
    detector.add_holding("688102", 35.95, 800, 36.50)  # 盈利1.53%
    detector.add_holding("601698", 29.98, 800, 29.97)  # 亏损-0.03%

    print("\n初始状态:")
    detector.print_status()

    # 价格变动
    print("\n模拟价格变动...")
    detector.update_price("603667", 65.31)  # 盈利0.01%，接近转亏
    detector.update_price("601698", 29.95)  # 继续亏损
    detector.update_price("688102", 37.00)  # 继续盈利

    print("\n更新后状态:")
    detector.print_status()

    # 检测紧急信号
    print("\n🔴 P0优先级紧急卖出信号:")
    signals = detector.generate_urgent_sell_signals()
    for code, score, reason in signals:
        print(f"  {reason}")
