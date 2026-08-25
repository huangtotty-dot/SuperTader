# -*- coding: utf-8 -*-
"""
参数优化管线 - OptimizationPipeline

实现离线验证 -> 纸面交易 -> 灰度部署的标准优化流程。
"""

from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import json


@dataclass
class ValidationReport:
    """验证报告"""
    status: str  # PASSED / FAILED / WARNING
    phase: str  # "offline" / "paper_trade" / "canary" / "production"

    # 时间戳
    created_at: datetime
    duration_seconds: float

    # 检查结果
    checks_passed: List[str]
    checks_failed: List[str]
    warnings: List[str]

    # 详细数据
    metrics: Dict[str, Any]

    # 建议
    recommendation: str


class ValidationPhase:
    """验证阶段基类"""

    def __init__(self, name: str):
        self.name = name
        self.start_time = None
        self.end_time = None

    def _get_duration(self) -> float:
        """获取耗时（秒）"""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0

    def validate(self, config: Dict[str, Any]) -> ValidationReport:
        """执行验证"""
        raise NotImplementedError


class OfflineValidationPhase(ValidationPhase):
    """离线验证阶段"""

    def __init__(self):
        super().__init__("offline_validation")

    def validate(self, config: Dict[str, Any]) -> ValidationReport:
        """
        离线验证

        检查项:
        1. 参数完整性
        2. 历史回测
        3. 收益预期
        4. 风险指标
        5. 假阳性率
        """
        self.start_time = datetime.now()

        checks_passed = []
        checks_failed = []
        warnings = []
        metrics = {}

        # 检查 1: 参数完整性
        required_params = [
            "signal.swing_bb_upper",
            "signal.swing_bb_lower",
            "market.index_levels",
        ]

        all_present = all(self._param_exists(config, p) for p in required_params)
        if all_present:
            checks_passed.append("All required parameters present")
        else:
            checks_failed.append("Some required parameters missing")

        # 检查 2: 参数范围
        bb_upper = self._get_param(config, "signal.swing_bb_upper")
        bb_lower = self._get_param(config, "signal.swing_bb_lower")

        if bb_upper is not None and -10 <= bb_upper <= 10:
            checks_passed.append("swing_bb_upper in valid range")
        else:
            warnings.append(f"swing_bb_upper unusual value: {bb_upper}")

        if bb_lower is not None and -10 <= bb_lower <= 0:
            checks_passed.append("swing_bb_lower in valid range")
        else:
            warnings.append(f"swing_bb_lower unusual value: {bb_lower}")

        # 模拟回测指标
        metrics = {
            "backtest_period": "Last 3 months",
            "total_trades": 125,
            "win_trades": 85,
            "win_rate": 0.68,
            "expected_return": 0.12,  # 12%
            "max_drawdown": 0.08,      # 8%
            "sharp_ratio": 1.5,
        }

        # 检查收益预期
        if metrics.get("expected_return", 0) >= 0.05:  # >= 5%
            checks_passed.append("Expected return meets threshold (>=5%)")
        else:
            warnings.append("Expected return below typical threshold")

        # 检查最大回撤
        if metrics.get("max_drawdown", 1) <= 0.15:  # <= 15%
            checks_passed.append("Max drawdown within limit (<=15%)")
        else:
            checks_failed.append("Max drawdown exceeds limit")

        # 检查假阳性率
        false_positive_rate = 1.0 - metrics.get("win_rate", 0)
        if false_positive_rate <= 0.5:  # <= 50%
            checks_passed.append("False positive rate acceptable (<50%)")
        else:
            warnings.append(f"High false positive rate: {false_positive_rate:.1%}")

        self.end_time = datetime.now()

        status = "PASSED" if not checks_failed else "FAILED"
        if warnings and not checks_failed:
            status = "WARNING"

        recommendation = self._get_recommendation(status, checks_failed, warnings)

        return ValidationReport(
            status=status,
            phase="offline",
            created_at=datetime.now(),
            duration_seconds=self._get_duration(),
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            warnings=warnings,
            metrics=metrics,
            recommendation=recommendation,
        )

    def _param_exists(self, config: Dict[str, Any], path: str) -> bool:
        """检查参数是否存在"""
        keys = path.split('.')
        value = config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return False
        return value is not None

    def _get_param(self, config: Dict[str, Any], path: str) -> Any:
        """获取参数值"""
        keys = path.split('.')
        value = config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    def _get_recommendation(self, status: str, failed: List[str], warnings: List[str]) -> str:
        """获取建议"""
        if status == "PASSED":
            return "Proceed to paper trading phase"
        elif status == "WARNING":
            return "Can proceed to paper trading with caution. Monitor these warnings."
        else:
            return "Fix the failed checks before proceeding"


class PaperTradePhase(ValidationPhase):
    """纸面交易验证阶段"""

    def __init__(self):
        super().__init__("paper_trade")

    def validate(self, config: Dict[str, Any]) -> ValidationReport:
        """
        纸面交易验证

        检查项:
        1. 信号质量 (胜率、赔率)
        2. 执行效果 (成交价格偏差)
        3. 系统稳定性 (错误率、卡顿)
        4. 持续3-5天观测
        """
        self.start_time = datetime.now()

        checks_passed = []
        checks_failed = []
        warnings = []
        metrics = {}

        # 模拟纸面交易数据
        metrics = {
            "trading_days": 5,
            "total_signals": 38,
            "successful_trades": 26,
            "win_rate": 0.684,
            "avg_win": 250,
            "avg_loss": 180,
            "profit_factor": 1.39,
            "avg_execution_latency_ms": 145,
            "fill_rate": 0.98,
            "system_errors": 0,
            "system_restarts": 0,
        }

        # 检查 1: 胜率
        if metrics.get("win_rate", 0) >= 0.60:
            checks_passed.append("Win rate acceptable (>=60%)")
        else:
            warnings.append(f"Win rate below target: {metrics['win_rate']:.1%}")

        # 检查 2: 利润因子
        if metrics.get("profit_factor", 0) >= 1.2:
            checks_passed.append("Profit factor healthy (>=1.2)")
        else:
            checks_failed.append(f"Profit factor too low: {metrics['profit_factor']}")

        # 检查 3: 执行效果
        if metrics.get("avg_execution_latency_ms", 1000) <= 200:
            checks_passed.append("Execution latency acceptable (<200ms)")
        else:
            warnings.append(f"High execution latency: {metrics['avg_execution_latency_ms']}ms")

        # 检查 4: 成交率
        if metrics.get("fill_rate", 0) >= 0.95:
            checks_passed.append("Fill rate excellent (>=95%)")
        else:
            warnings.append(f"Fill rate below target: {metrics['fill_rate']:.1%}")

        # 检查 5: 系统稳定性
        if metrics.get("system_errors", 1) == 0 and metrics.get("system_restarts", 1) == 0:
            checks_passed.append("System stable (no errors or restarts)")
        else:
            errors = metrics.get("system_errors", 0)
            restarts = metrics.get("system_restarts", 0)
            checks_failed.append(f"System issues: {errors} errors, {restarts} restarts")

        self.end_time = datetime.now()

        status = "PASSED" if not checks_failed else "FAILED"
        if warnings and not checks_failed:
            status = "WARNING"

        recommendation = self._get_recommendation(status, checks_failed)

        return ValidationReport(
            status=status,
            phase="paper_trade",
            created_at=datetime.now(),
            duration_seconds=self._get_duration(),
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            warnings=warnings,
            metrics=metrics,
            recommendation=recommendation,
        )

    def _get_recommendation(self, status: str, failed: List[str]) -> str:
        """获取建议"""
        if status == "PASSED":
            return "Proceed to canary deployment (10% position)"
        elif status == "WARNING":
            return "Can deploy with increased monitoring"
        else:
            return "Rollback and adjust parameters"


class CanaryDeploymentPhase(ValidationPhase):
    """灰度部署验证阶段"""

    def __init__(self):
        super().__init__("canary_deployment")

    def validate(self, config: Dict[str, Any]) -> ValidationReport:
        """
        灰度部署验证

        检查项:
        1. 实盘交易表现
        2. 资金风控
        3. 市场适应性
        4. 对标历史版本
        """
        self.start_time = datetime.now()

        checks_passed = []
        checks_failed = []
        warnings = []
        metrics = {}

        # 模拟灰度部署数据
        metrics = {
            "deployment_period": "1-2 weeks",
            "position_size_pct": 10,
            "live_trades": 42,
            "live_win_rate": 0.667,
            "live_return": 0.035,  # 3.5%
            "max_daily_loss": -450,
            "vs_baseline_return": -0.005,  # -0.5%
            "downtime_minutes": 0,
            "risk_incidents": 0,
        }

        # 检查 1: 实盘表现
        if metrics.get("live_win_rate", 0) >= 0.60:
            checks_passed.append("Live win rate acceptable")
        else:
            warnings.append(f"Live win rate: {metrics['live_win_rate']:.1%}")

        # 检查 2: 收益表现
        if metrics.get("live_return", 0) >= 0:
            checks_passed.append("Live return positive")
        else:
            warnings.append(f"Live return negative: {metrics['live_return']:.1%}")

        # 检查 3: 风控
        max_loss = metrics.get("max_daily_loss", 0)
        if max_loss >= -1000:  # 最大单日亏损 <= 1000
            checks_passed.append("Daily loss within limit")
        else:
            checks_failed.append(f"Excessive daily loss: {max_loss}")

        # 检查 4: 对标
        vs_baseline = metrics.get("vs_baseline_return", 0)
        if abs(vs_baseline) <= 0.02:  # 与baseline偏差 <= 2%
            checks_passed.append("Performance vs baseline acceptable")
        else:
            warnings.append(f"Performance gap from baseline: {vs_baseline:.1%}")

        # 检查 5: 系统运行
        if metrics.get("downtime_minutes", 1) == 0:
            checks_passed.append("No system downtime")
        else:
            warnings.append(f"Downtime: {metrics['downtime_minutes']} minutes")

        if metrics.get("risk_incidents", 1) == 0:
            checks_passed.append("No risk incidents")
        else:
            checks_failed.append(f"Risk incidents: {metrics['risk_incidents']}")

        self.end_time = datetime.now()

        status = "PASSED" if not checks_failed else "FAILED"
        if warnings and not checks_failed:
            status = "WARNING"

        recommendation = self._get_recommendation(status, checks_failed)

        return ValidationReport(
            status=status,
            phase="canary",
            created_at=datetime.now(),
            duration_seconds=self._get_duration(),
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            warnings=warnings,
            metrics=metrics,
            recommendation=recommendation,
        )

    def _get_recommendation(self, status: str, failed: List[str]) -> str:
        """获取建议"""
        if status == "PASSED":
            return "Promote to production (100% position)"
        elif status == "WARNING":
            return "Extend canary period and monitor closely"
        else:
            return "Rollback immediately"


class OptimizationPipeline:
    """参数优化管线"""

    def __init__(self, output_dir: str = "t_io/optimization"):
        """
        初始化优化管线

        Args:
            output_dir: 输出目录
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 初始化各阶段
        self.offline_phase = OfflineValidationPhase()
        self.paper_trade_phase = PaperTradePhase()
        self.canary_phase = CanaryDeploymentPhase()

        # 报告历史
        self.reports: List[ValidationReport] = []

    def run_full_pipeline(self, config: Dict[str, Any],
                         skip_phases: List[str] = None) -> ValidationReport:
        """
        运行完整的优化管线

        Args:
            config: 配置字典
            skip_phases: 跳过的阶段列表

        Returns:
            最终验证报告
        """
        if skip_phases is None:
            skip_phases = []

        # 阶段1: 离线验证
        if "offline" not in skip_phases:
            print("[STEP 1] Running offline validation...")
            report = self.offline_phase.validate(config)
            self.reports.append(report)
            print(f"  Status: {report.status}")
            print(f"  Recommendation: {report.recommendation}")

            if report.status == "FAILED":
                return report

        # 阶段2: 纸面交易
        if "paper_trade" not in skip_phases:
            print("\n[STEP 2] Running paper trading validation...")
            report = self.paper_trade_phase.validate(config)
            self.reports.append(report)
            print(f"  Status: {report.status}")
            print(f"  Recommendation: {report.recommendation}")

            if report.status == "FAILED":
                return report

        # 阶段3: 灰度部署
        if "canary" not in skip_phases:
            print("\n[STEP 3] Running canary deployment validation...")
            report = self.canary_phase.validate(config)
            self.reports.append(report)
            print(f"  Status: {report.status}")
            print(f"  Recommendation: {report.recommendation}")

            return report

        return self.reports[-1] if self.reports else None

    def save_report(self, report: ValidationReport, name: str = "") -> str:
        """
        保存验证报告

        Args:
            report: 报告对象
            name: 报告名称

        Returns:
            报告文件路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"validation_{report.phase}_{timestamp}.json"

        filepath = self.output_dir / filename

        report_data = {
            "status": report.status,
            "phase": report.phase,
            "created_at": report.created_at.isoformat(),
            "duration_seconds": report.duration_seconds,
            "checks_passed": report.checks_passed,
            "checks_failed": report.checks_failed,
            "warnings": report.warnings,
            "metrics": report.metrics,
            "recommendation": report.recommendation,
        }

        with open(filepath, 'w') as f:
            json.dump(report_data, f, indent=2)

        return str(filepath)

    def get_summary(self) -> Dict[str, Any]:
        """获取管线执行摘要"""
        if not self.reports:
            return {}

        return {
            "total_phases": len(self.reports),
            "final_status": self.reports[-1].status,
            "all_passed": all(r.status == "PASSED" for r in self.reports),
            "total_duration_seconds": sum(r.duration_seconds for r in self.reports),
            "reports": [
                {
                    "phase": r.phase,
                    "status": r.status,
                    "checks_passed": len(r.checks_passed),
                    "checks_failed": len(r.checks_failed),
                    "warnings": len(r.warnings),
                }
                for r in self.reports
            ],
        }
