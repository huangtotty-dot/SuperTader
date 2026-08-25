# -*- coding: utf-8 -*-
"""
交易系统协调器 - TradingSystemCoordinator

整合所有系统组件（数据、分析、决策、执行），形成完整的交易闭环。
"""

from typing import Dict, List, Any, Optional
from datetime import datetime
import json
from pathlib import Path

from core.config_manager import ConfigManager
from core.config_validator import ConfigValidator
from core.metrics_tracker import MetricsTracker
from core.optimization_pipeline import OptimizationPipeline
from core.dto import Signal, Order, OrderStatus, MarketData


class TradingSystemCoordinator:
    """交易系统协调器 - 统一协调各层组件"""

    def __init__(self, config_dir: str = "config"):
        """
        初始化交易系统协调器

        Args:
            config_dir: 配置目录
        """
        # 初始化配置管理
        self.config_manager = ConfigManager(config_dir=config_dir)
        self.config_validator = ConfigValidator()

        # 初始化指标跟踪
        self.metrics_tracker = MetricsTracker()

        # 初始化优化管线
        self.optimization_pipeline = OptimizationPipeline()

        # 系统状态
        self.is_running = False
        self.current_config = {}
        self.system_state = {
            "status": "INITIALIZED",
            "last_updated": datetime.now().isoformat(),
            "holdings": {},
            "active_signals": [],
            "pending_orders": [],
        }

        # 数据流管道
        self.market_data_queue: List[MarketData] = []
        self.signal_queue: List[Signal] = []
        self.order_queue: List[Order] = []

    def initialize_system(self, config_version: str = "v2.0_current_20260825") -> bool:
        """
        初始化系统

        Args:
            config_version: 配置版本

        Returns:
            是否初始化成功
        """
        print("[SYSTEM] Initializing trading system...")

        try:
            # 加载配置
            print("  [1/4] Loading configuration...")
            self.current_config = self.config_manager.load_version(config_version)

            # 验证配置
            print("  [2/4] Validating configuration...")
            is_valid, errors = self.config_validator.validate(self.current_config)
            if not is_valid:
                print("  [ERROR] Configuration validation failed:")
                for error in errors:
                    print(f"    - {error.path}: {error.message}")
                return False

            # 运行优化管线验证
            print("  [3/4] Running optimization pipeline...")
            final_report = self.optimization_pipeline.run_full_pipeline(self.current_config)
            if final_report.status != "PASSED":
                print(f"  [WARNING] Pipeline status: {final_report.status}")

            # 系统就绪
            print("  [4/4] System ready")
            self.system_state["status"] = "READY"
            self.system_state["last_updated"] = datetime.now().isoformat()

            print("[OK] Trading system initialized successfully")
            return True

        except Exception as e:
            print(f"[ERROR] Initialization failed: {e}")
            self.system_state["status"] = "ERROR"
            return False

    def start_trading(self) -> bool:
        """启动交易系统"""
        if self.system_state["status"] != "READY":
            print("[ERROR] System not ready. Call initialize_system() first")
            return False

        print("[SYSTEM] Starting trading system...")
        self.is_running = True
        self.system_state["status"] = "RUNNING"
        self.system_state["last_updated"] = datetime.now().isoformat()

        print("[OK] Trading system started")
        return True

    def stop_trading(self) -> bool:
        """停止交易系统"""
        print("[SYSTEM] Stopping trading system...")
        self.is_running = False

        # 保存最终指标
        report_file = self.metrics_tracker.save_daily_report()
        print(f"  Daily report saved: {report_file}")

        self.system_state["status"] = "STOPPED"
        self.system_state["last_updated"] = datetime.now().isoformat()

        print("[OK] Trading system stopped")
        return True

    def process_market_data(self, market_data: MarketData) -> None:
        """
        处理市场数据

        Args:
            market_data: 市场数据
        """
        if not self.is_running:
            return

        self.market_data_queue.append(market_data)

    def generate_signal(self, code: str, signal_type: str, price: float,
                       strength: float, reason: str = "") -> Signal:
        """
        生成交易信号

        Args:
            code: 股票代码
            signal_type: 信号类型
            price: 信号价格
            strength: 信号强度
            reason: 信号原因

        Returns:
            Signal 对象
        """
        signal = Signal(
            code=code,
            signal_type=signal_type,
            timestamp=datetime.now(),
            strength=strength,
            reason=reason,
            price=price,
            decision_time=datetime.now().strftime("%H:%M"),
        )

        self.signal_queue.append(signal)
        self.metrics_tracker.record_signal(code, signal_type, price, strength, reason)

        return signal

    def execute_order(self, order: Order) -> bool:
        """
        执行订单

        Args:
            order: 订单对象

        Returns:
            是否成功提交
        """
        if not self.is_running:
            print("[ERROR] System not running")
            return False

        self.order_queue.append(order)
        self.metrics_tracker.record_order(
            code=order.code,
            direction=order.direction,
            qty=order.quantity,
            price=order.price,
            order_id=order.order_id or f"ORD_{datetime.now().timestamp()}",
            status="SUBMITTED",
        )

        return True

    def get_config(self, path: str, default: Any = None) -> Any:
        """获取配置参数"""
        return self.config_manager.get(path, default)

    def set_config(self, path: str, value: Any) -> None:
        """设置配置参数"""
        self.config_manager.set(path, value)

    def validate_config(self) -> bool:
        """验证当前配置"""
        is_valid, errors = self.config_validator.validate(self.current_config)
        if not is_valid:
            print("[ERROR] Configuration validation failed:")
            for error in errors:
                print(f"  - {error.path}: {error.message}")
        return is_valid

    def save_config_snapshot(self, name: str, description: str = "") -> str:
        """保存配置快照"""
        return self.config_manager.save_snapshot(name, description)

    def get_system_status(self) -> Dict[str, Any]:
        """获取系统状态"""
        return {
            "timestamp": datetime.now().isoformat(),
            "system_state": self.system_state,
            "metrics": self.metrics_tracker.get_summary(),
            "queues": {
                "market_data_pending": len(self.market_data_queue),
                "signals_pending": len(self.signal_queue),
                "orders_pending": len(self.order_queue),
            },
        }

    def get_holdings(self) -> Dict[str, int]:
        """获取当前持仓"""
        return self.system_state.get("holdings", {})

    def update_holdings(self, holdings: Dict[str, int]) -> None:
        """更新持仓"""
        self.system_state["holdings"] = holdings.copy()
        self.metrics_tracker.update_position(holdings)

    def save_system_state(self, filepath: str = "t_io/system_state.json") -> str:
        """保存系统状态快照"""
        state_file = Path(filepath)
        state_file.parent.mkdir(parents=True, exist_ok=True)

        state_data = {
            "timestamp": datetime.now().isoformat(),
            "system_state": self.system_state,
            "current_config": self.current_config,
            "metrics": self.metrics_tracker.get_summary(),
        }

        with open(state_file, 'w') as f:
            json.dump(state_data, f, indent=2, default=str)

        return str(state_file)

    def generate_daily_report(self) -> str:
        """生成日报告"""
        return self.metrics_tracker.save_daily_report()

    def get_diagnostics(self) -> Dict[str, Any]:
        """获取系统诊断信息"""
        return {
            "system_status": self.system_state["status"],
            "is_running": self.is_running,
            "config_version": self.config_manager.current_version,
            "config_hash": self.config_manager.config_hash,
            "metrics_summary": self.metrics_tracker.get_summary(),
            "optimization_reports": len(self.optimization_pipeline.reports),
            "market_data_queue_size": len(self.market_data_queue),
            "signal_queue_size": len(self.signal_queue),
            "order_queue_size": len(self.order_queue),
        }
